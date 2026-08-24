import { useState } from "react";
import { useMe } from "@/hooks/useMe";
import { useUnlinkIdentity } from "@/hooks/useMe";
import { useAuthProviders } from "@/hooks/useAuthProviders";
import { useGenerateToken } from "@/hooks/useToken";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const PROVIDER_LABELS: Record<string, string> = {
  github: "GitHub",
  steam: "Steam",
};

export function AccountPage() {
  const { data: me } = useMe();
  const { data: providers } = useAuthProviders();
  const unlinkIdentity = useUnlinkIdentity();
  const generateToken = useGenerateToken();
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const linkedProviders = new Set((me?.identities ?? []).map((i) => i.provider));
  const linkable = Object.entries(providers ?? {})
    .filter(([slug, on]) => on && !linkedProviders.has(slug))
    .map(([slug]) => slug);

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h1 className="text-xl font-semibold">My account</h1>
        <div className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span>{me?.display_name ?? me?.username}</span>
          {me?.is_admin ? <Badge>Admin</Badge> : null}
        </div>
      </div>
      <div className="space-y-2">
        <h2 className="text-sm font-medium">Linked identities</h2>
        <p className="text-sm text-muted-foreground">
          Sign in with either linked identity — they reach the same account.
        </p>
        <ul className="space-y-1">
          {(me?.identities ?? []).map((identity) => (
            <li key={identity.id} className="flex items-center justify-between text-sm">
              <span>
                {PROVIDER_LABELS[identity.provider] ?? identity.provider}:{" "}
                {identity.display_name ?? identity.raw_id}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={unlinkIdentity.isPending || (me?.identities.length ?? 0) <= 1}
                onClick={() => unlinkIdentity.mutate(identity.id)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
        {unlinkIdentity.isError ? (
          <p className="text-sm text-destructive">{unlinkIdentity.error.message}</p>
        ) : null}
        {linkable.length > 0 ? (
          <div className="flex gap-2 pt-1">
            {linkable.map((slug) => (
              <a
                key={slug}
                href={`/admin/link/${slug}`}
                className="rounded-md border px-3 py-1.5 text-sm"
              >
                Link {PROVIDER_LABELS[slug] ?? slug}
              </a>
            ))}
          </div>
        ) : null}
      </div>
      <div className="space-y-2">
        <h2 className="text-sm font-medium">Personal token</h2>
        <p className="text-sm text-muted-foreground">
          Use this for MCP clients that can't do a browser login (e.g. Claude
          Desktop). Generating a new token immediately invalidates any
          previous one.
        </p>
        <Button
          onClick={async () => {
            const result = await generateToken.mutateAsync();
            setToken(result.token);
            setCopied(false);
          }}
          disabled={generateToken.isPending}
        >
          {token ? "Regenerate token" : "Generate token"}
        </Button>
        {generateToken.isError ? (
          <p className="text-sm text-destructive">{generateToken.error.message}</p>
        ) : null}
        {token ? (
          <div className="space-y-1">
            <p className="text-sm font-medium">Copy this now — it won't be shown again:</p>
            <pre className="overflow-x-auto rounded-md border bg-muted p-2 text-xs">{token}</pre>
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                await navigator.clipboard.writeText(token);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? "Copied!" : "Copy"}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
