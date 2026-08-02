import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";
import { meQueryOptions } from "@/hooks/useMe";
import { AppLayout } from "@/components/AppLayout";
import { LoginPage } from "@/components/LoginPage";
import { ServersPage } from "@/components/ServersPage";
import { LogsPage } from "@/components/LogsPage";

interface RouterContext {
  queryClient: QueryClient;
}

export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: () => <Outlet />,
});

export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (search: Record<string, unknown>): { error?: string } => ({
    error: typeof search.error === "string" ? search.error : undefined,
  }),
  component: LoginPage,
});

const authedLayoutRoute = createRoute({
  id: "_authed",
  getParentRoute: () => rootRoute,
  beforeLoad: async ({ context }) => {
    try {
      await context.queryClient.ensureQueryData(meQueryOptions);
    } catch {
      throw redirect({ to: "/login" });
    }
  },
  component: AppLayout,
});

export const serversRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/",
  component: ServersPage,
});

export const logsRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/logs",
  component: LogsPage,
});

export const testerRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/tester",
  component: () => <p>Tool tester placeholder — replaced in Task 10</p>,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  authedLayoutRoute.addChildren([serversRoute, logsRoute, testerRoute]),
]);

export function createAppRouter(queryClient: QueryClient) {
  return createRouter({
    routeTree,
    basepath: "/admin",
    context: { queryClient },
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
