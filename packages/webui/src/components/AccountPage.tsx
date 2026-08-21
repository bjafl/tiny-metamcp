import { useState } from "react";
import { useMe } from "@/hooks/useMe";
import { useGenerateToken } from "@/hooks/useToken";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function AccountPage() {
  const { data: me } = useMe();
  const generateToken = useGenerateToken();
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h1 className="text-xl font-semibold">My account</h1>
        <div className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span>{me?.username}</span>
          {me?.is_admin ? <Badge>Admin</Badge> : null}
        </div>
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
