import { loginRoute } from "@/router";
import { useAuthProviders } from "@/hooks/useAuthProviders";

const PROVIDER_LABELS: Record<string, string> = {
  github: "GitHub",
  steam: "Steam",
};

export function LoginPage() {
  const { error } = loginRoute.useSearch();
  const { data: providers } = useAuthProviders();
  const enabled = Object.entries(providers ?? {}).filter(([, on]) => on);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-full max-w-sm space-y-4 text-center">
        <h1 className="text-2xl font-semibold">MCP Aggregator</h1>
        <p className="text-muted-foreground">Sign in to access the admin interface.</p>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <div className="space-y-2">
          {enabled.map(([slug]) => (
            <a
              key={slug}
              href={`/admin/login/${slug}`}
              className="block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
            >
              Login with {PROVIDER_LABELS[slug] ?? slug}
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
