import { Link, Outlet } from "@tanstack/react-router";
import { useMe } from "@/hooks/useMe";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/ThemeToggle";

export function AppLayout() {
  const { data: me } = useMe();

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b">
        <nav className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-6">
            <span className="font-semibold">MCP Aggregator</span>
            <Link
              to="/"
              activeOptions={{ exact: true }}
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Servers
            </Link>
            <Link
              to="/logs"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Logs
            </Link>
            <Link
              to="/tester"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Tool Tester
            </Link>
            <Link
              to="/account"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Account
            </Link>
          </div>
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <ThemeToggle />
            <span>{me?.username}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                window.location.href = "/admin/logout";
              }}
            >
              Logout
            </Button>
          </div>
        </nav>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
