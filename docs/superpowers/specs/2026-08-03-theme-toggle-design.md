# Design: light/dark/system theme toggle (admin webui)

**Date:** 2026-08-03
**Status:** Approved, not yet implemented

## Context

`packages/webui` (the React admin SPA) already ships shadcn/Tailwind v4
CSS-variable tokens for both a light and a dark palette (`:root` and
`.dark` blocks in `src/index.css`, wired via
`@custom-variant dark (&:is(.dark *));`), but nothing ever adds the
`.dark` class — the app is permanently light. The goal: a theme toggle in
the admin UI with three states — light, dark, system (system = follow the
OS preference) — defaulting to system.

## Design

### State: `ThemeProvider` (`src/hooks/useTheme.tsx`)

A small React context, not the `next-themes` package (that's Next.js-only
and this is a plain Vite SPA — a ~40-line custom provider is simpler than
pulling in a dependency built for a different framework):

- `theme: "light" | "dark" | "system"`, persisted to
  `localStorage["theme"]`. No stored value → `"system"`.
- Resolves `theme` to an actual light/dark value (`"system"` reads
  `matchMedia("(prefers-color-scheme: dark)")`) and toggles the `dark`
  class on `document.documentElement` accordingly.
- While `theme === "system"`, subscribes to the media query's `change`
  event so an OS-level theme switch updates the app live, without a
  reload.
- Exposes `{ theme, setTheme, cycleTheme }` via a `useTheme()` hook.

### FOUC prevention (`index.html`)

A small inline `<script>` in the `<head>`, before the app bundle loads,
reads `localStorage["theme"]` (falling back to the media query for
`"system"`/no value) and sets the `dark` class immediately. Without this,
every reload would flash light mode first regardless of the stored
preference, since React/the provider only runs after the initial paint.

### UI (`src/components/ThemeToggle.tsx`)

One icon button (lucide-react `Sun`/`Moon`/`Monitor`, matching the icon to
the current `theme` value — not the resolved light/dark, so "system" is
visibly distinguishable from an explicit light/dark choice) in
`AppLayout`'s header, to the left of the username/logout group. Click
cycles `light → dark → system → light` via `cycleTheme()`.

### Wiring

- `main.tsx`: wrap `<RouterProvider>` in `<ThemeProvider>`.
- `AppLayout.tsx`: render `<ThemeToggle />` in the header's right-hand
  button group.

No backend changes, no new dependencies (lucide-react is already a
dependency).

## Testing

No test suite exists for the webui (confirmed in `CLAUDE.md`/prior
session work — no vitest/frontend test runner configured). Verify
manually via `just webui-dev`: default is system on first load with no
stored preference; each of the three states renders the correct
icon and applies/removes `.dark` correctly; the choice survives a page
reload; toggling the OS theme while `theme === "system"` updates the app
live without a reload.
