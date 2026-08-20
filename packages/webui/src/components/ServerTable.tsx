import { useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StatusBadge } from "@/components/StatusBadge";
import { AddServerDialog } from "@/components/AddServerDialog";
import {
  useDeleteServer,
  useDisableServer,
  useEnableServer,
  useRestartServer,
  useEditServer,
} from "@/hooks/useServers";
import { useMe } from "@/hooks/useMe";
import type { ServerConfig, ServerVisibility } from "@/lib/types";

export function ServerTable({ servers }: { servers: ServerConfig[] }) {
  const { data: me } = useMe();
  const enableServer = useEnableServer();
  const disableServer = useDisableServer();
  const restartServer = useRestartServer();
  const deleteServer = useDeleteServer();
  const editServer = useEditServer();
  const [editingServer, setEditingServer] = useState<ServerConfig | null>(null);
  // Holds the most recently edited server and is never reset to null once
  // set, unlike `editingServer` -- AddServerDialog's props require `server`
  // whenever `open`/`onOpenChange` are passed, so this keeps it mounted
  // (open toggling via `editingServer`) instead of remounting it on every
  // close, which would lose Radix's exit animation.
  const [dialogServer, setDialogServer] = useState<ServerConfig | null>(null);

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Package</TableHead>
            <TableHead>Owner</TableHead>
            <TableHead>Visibility</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {servers.map((s) => (
            <TableRow key={s.id}>
              <TableCell className="font-medium">{s.name}</TableCell>
              <TableCell>{s.type}</TableCell>
              <TableCell className="max-w-xs truncate">{s.package}</TableCell>
              <TableCell className="text-muted-foreground">{s.owner ?? "—"}</TableCell>
              <TableCell>
                {me?.is_admin ? (
                  <Select
                    value={s.visibility}
                    onValueChange={(v) =>
                      editServer.mutate({
                        id: s.id,
                        input: { visibility: v as ServerVisibility },
                      })
                    }
                  >
                    <SelectTrigger size="sm">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="private">Private</SelectItem>
                      <SelectItem value="everyone">Everyone</SelectItem>
                    </SelectContent>
                  </Select>
                ) : (
                  <Badge variant={s.visibility === "everyone" ? "outline" : "secondary"}>
                    {s.visibility === "everyone" ? "Everyone" : "Private"}
                  </Badge>
                )}
              </TableCell>
              <TableCell>
                <StatusBadge server={s} />
                {s.error ? (
                  <p className="mt-1 text-xs text-destructive">{s.error}</p>
                ) : null}
              </TableCell>
              <TableCell className="space-x-2 text-right">
                {s.enabled ? (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => restartServer.mutate(s.id)}
                    >
                      Restart
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => disableServer.mutate(s.id)}
                    >
                      Disable
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => enableServer.mutate(s.id)}
                  >
                    Enable
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setDialogServer(s);
                    setEditingServer(s);
                  }}
                >
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={() => {
                    if (confirm(`Delete server "${s.name}"?`)) {
                      deleteServer.mutate(s.id);
                    }
                  }}
                >
                  Delete
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {dialogServer ? (
        <AddServerDialog
          server={dialogServer}
          open={editingServer !== null}
          onOpenChange={(o) => {
            if (!o) setEditingServer(null);
          }}
        />
      ) : null}
    </>
  );
}
