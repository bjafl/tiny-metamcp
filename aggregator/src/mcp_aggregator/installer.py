import asyncio
import logging
import shutil

from .config import PACKAGES_DIR
from .database import ServerConfig, ServerType

logger = logging.getLogger(__name__)


def build_command(config: ServerConfig) -> list[str]:
    """Return the subprocess command list to launch a child MCP server."""
    if config.type == ServerType.PYPI:
        # uvx installs and runs the package in an isolated venv
        return ["uvx", config.package, *config.args]

    if config.type == ServerType.NPM:
        return ["npx", "--yes", config.package, *config.args]

    if config.type == ServerType.GIT:
        clone_dir = PACKAGES_DIR / config.name
        if (clone_dir / "pyproject.toml").exists() or (clone_dir / "setup.py").exists():
            return ["uvx", "--from", str(clone_dir), config.package.split("/")[-1], *config.args]
        if (clone_dir / "package.json").exists():
            entry = _npm_main(clone_dir)
            return ["node", entry, *config.args]
        raise RuntimeError(f"Cannot detect runtime for git repo at {clone_dir}")

    if config.type == ServerType.CMD:
        return [*config.package.split(), *config.args]

    raise ValueError(f"Unknown server type: {config.type}")


def _npm_main(clone_dir) -> str:
    import json
    pkg = json.loads((clone_dir / "package.json").read_text())
    main = pkg.get("main", "index.js")
    return str(clone_dir / main)


async def install(config: ServerConfig) -> None:
    """Run any pre-installation step required before first launch."""
    if config.type == ServerType.GIT:
        await _git_clone(config)
    # PyPI (uvx) and npm (npx) are self-installing on first run


async def _git_clone(config: ServerConfig) -> None:
    clone_dir = PACKAGES_DIR / config.name
    if clone_dir.exists():
        logger.info("Git repo already cloned: %s", clone_dir)
        return

    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning %s → %s", config.package, clone_dir)
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth=1", config.package, str(clone_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {stderr.decode().strip()}")


async def uninstall(config: ServerConfig) -> None:
    """Remove any locally installed files (git repos only)."""
    if config.type == ServerType.GIT:
        clone_dir = PACKAGES_DIR / config.name
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
            logger.info("Removed %s", clone_dir)
