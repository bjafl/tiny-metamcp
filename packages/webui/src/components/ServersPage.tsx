import { useServers } from "@/hooks/useServers";
import { ServerTable } from "@/components/ServerTable";
import { AddServerDialog } from "@/components/AddServerDialog";

export function ServersPage() {
  const { data, isLoading, error } = useServers();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Servers</h1>
        <AddServerDialog />
      </div>
      {isLoading ? <p className="text-muted-foreground">Loading servers…</p> : null}
      {error ? <p className="text-destructive">{error.message}</p> : null}
      {data ? <ServerTable servers={data} /> : null}
    </div>
  );
}
