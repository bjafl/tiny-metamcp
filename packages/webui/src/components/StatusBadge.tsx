import { Badge } from "@/components/ui/badge";
import type { ServerConfig } from "@/lib/types";

export function StatusBadge({ server }: { server: ServerConfig }) {
  if (!server.enabled) return <Badge variant="secondary">Disabled</Badge>;
  if (server.error) return <Badge variant="destructive">Error</Badge>;
  if (server.running) {
    return (
      <Badge className="bg-emerald-600 hover:bg-emerald-600">
        Running ({server.tool_count})
      </Badge>
    );
  }
  return <Badge variant="outline">Starting…</Badge>;
}
