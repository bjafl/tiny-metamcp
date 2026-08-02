import { loginRoute } from "@/router";

export function LoginPage() {
  const { error } = loginRoute.useSearch();
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-full max-w-sm space-y-4 text-center">
        <h1 className="text-2xl font-semibold">MCP Aggregator</h1>
        <p className="text-muted-foreground">
          Sign in with GitHub to access the admin interface.
        </p>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <a
          href="/admin/login/github"
          className="inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
        >
          Login with GitHub
        </a>
      </div>
    </div>
  );
}
