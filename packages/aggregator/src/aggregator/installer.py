import asyncio
import json
import logging
import shutil

from .config import PACKAGES_DIR
from .models import Server, ServerType

logger = logging.getLogger(__name__)


def build_command(config: Server) -> list[str]:
    """Return the subprocess command list to launch a child MCP server."""
    args = config.get_args()

    if config.type == ServerType.PYPI:
        return _uvx_command(config.package, args)

    if config.type == ServerType.NPM:
        return ["npx", "--yes", config.package, *args]

    if config.type == ServerType.GIT:
        clone_dir = PACKAGES_DIR / config.name
        if (clone_dir / "pyproject.toml").exists() or (clone_dir / "setup.py").exists():
            return _uvx_command(str(clone_dir), args)
        if (clone_dir / "package.json").exists():
            return ["node", _npm_main(clone_dir), *args]
        raise RuntimeError(f"Cannot detect runtime for git repo at {clone_dir}")

    if config.type == ServerType.CMD:
        return [*config.package.split(), *args]

    raise ValueError(f"Unknown server type: {config.type}")


def _uvx_command(source: str, args: list[str]) -> list[str]:
    """
    Build a uvx invocation.

    If args[0] is a non-flag string it is treated as the entrypoint name and
    --from is used so the install source and entrypoint can differ.  This
    supports PyPI packages whose name differs from the console script, git
    monorepo sub-packages (git+https://…#subdirectory=…), and local paths.

    Without an explicit entrypoint the package name is used directly, which is
    the common case for simple PyPI packages (uvx mcp-server-fetch).
    """
    if args and not args[0].startswith("-"):
        entrypoint, rest = args[0], args[1:]
        return ["uvx", "--from", source, entrypoint, *rest]
    return ["uvx", source, *args]


def _npm_main(clone_dir) -> str:
    pkg = json.loads((clone_dir / "package.json").read_text())
    return str(clone_dir / pkg.get("main", "index.js"))


async def install(config: Server) -> None:
    """Run any pre-installation step required before first launch."""
    if config.type == ServerType.GIT:
        await _git_clone(config)
    # PYPI (uvx) and NPM (npx) install themselves on first run


async def _git_clone(config: Server) -> None:
    clone_dir = PACKAGES_DIR / config.name
    if clone_dir.exists():
        logger.info("Git repo already cloned: %s", clone_dir)
        return

    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning %s → %s", config.package, clone_dir)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth=1",
        config.package,
        str(clone_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {stderr.decode().strip()}")

    if (clone_dir / "package.json").exists():
        await _npm_install_and_build(clone_dir)


async def _npm_install_and_build(clone_dir) -> None:
    logger.info("npm install: %s", clone_dir)
    proc = await asyncio.create_subprocess_exec(
        "npm",
        "install",
        "--prefer-offline",
        cwd=str(clone_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"npm install failed: {stderr.decode().strip()}")

    pkg = json.loads((clone_dir / "package.json").read_text())
    if "build" in pkg.get("scripts", {}):
        logger.info("npm run build: %s", clone_dir)
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "run",
            "build",
            cwd=str(clone_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"npm run build failed: {stderr.decode().strip()}")


async def uninstall(config: Server) -> None:
    """Remove locally installed files (git repos only)."""
    if config.type == ServerType.GIT:
        clone_dir = PACKAGES_DIR / config.name
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
            logger.info("Removed %s", clone_dir)
