# webui

Admin SPA for the MCP Aggregator. Built with Vite, React, TypeScript,
Tailwind CSS, shadcn/ui, and TanStack Router + Query.

In production this package is built to static assets and served by the
FastAPI backend (`packages/aggregator`) under `/admin`.

## Development

```bash
pnpm dev
```

`pnpm dev` proxies `/api`, `/admin/login/github`, `/admin/logout`, and
`/oauth/*` to a backend running locally on `http://localhost:8000` (see
`vite.config.ts`) — start the aggregator separately for these to work.

## Build

```bash
pnpm build
```

Type-checks with `tsc -b` and outputs static assets to `dist/`.
