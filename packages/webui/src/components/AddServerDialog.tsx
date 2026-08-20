import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAddServer, useEditServer } from "@/hooks/useServers";
import type { AddServerInput, ServerConfig, ServerType, ServerVisibility } from "@/lib/types";

function parseArgs(raw: string): string[] {
  return raw
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);
}

function parseEnv(raw: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const [k, ...rest] = pair.split("=");
    if (k && rest.length) env[k.trim()] = rest.join("=").trim();
  }
  return env;
}

function formatArgs(args: string[]): string {
  return args.join(", ");
}

function formatEnv(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
}

// `server`, `open`, and `onOpenChange` must all be provided together (edit
// mode, controlled by the caller) or all omitted (add mode, self-contained
// trigger button) -- this makes the otherwise-possible "edit-mode chrome
// with no server to edit" combination unrepresentable.
type AddServerDialogProps =
  | { server?: undefined; open?: undefined; onOpenChange?: undefined }
  | { server: ServerConfig; open: boolean; onOpenChange: (open: boolean) => void };

export function AddServerDialog({
  server,
  open: openProp,
  onOpenChange,
}: AddServerDialogProps = {}) {
  const controlled = onOpenChange !== undefined;
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlled ? (openProp ?? false) : internalOpen;
  const setOpen = controlled ? onOpenChange : setInternalOpen;

  const [name, setName] = useState("");
  const [type, setType] = useState<ServerType>("pypi");
  const [visibility, setVisibility] = useState<ServerVisibility>("private");
  const [pkg, setPkg] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [initialArgs, setInitialArgs] = useState("");
  const [initialEnv, setInitialEnv] = useState("");
  const addServer = useAddServer();
  const editServer = useEditServer();
  const mutation = controlled ? editServer : addServer;
  const resetEditServer = editServer.reset;
  const resetAddServer = addServer.reset;

  // Tracks which server (by id) the fields were last prefilled for, so a
  // background refetch that changes `server`'s identity without changing
  // which row is being edited doesn't wipe in-progress input.
  const prefilledForId = useRef<number | null>(null);

  useEffect(() => {
    if (!open) {
      prefilledForId.current = null;
      return;
    }
    if (server && prefilledForId.current === server.id) return;
    prefilledForId.current = server?.id ?? null;
    resetEditServer();
    resetAddServer();
    setName(server?.name ?? "");
    setType(server?.type ?? "pypi");
    setVisibility(server?.visibility ?? "private");
    setPkg(server?.package ?? "");
    const argsStr = server ? formatArgs(server.args) : "";
    const envStr = server ? formatEnv(server.env) : "";
    setArgs(argsStr);
    setEnv(envStr);
    setInitialArgs(argsStr);
    setInitialEnv(envStr);
  }, [open, server, resetEditServer, resetAddServer]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (controlled && server) {
      const payload: Partial<AddServerInput> = { name, type, package: pkg, visibility };
      if (args !== initialArgs) payload.args = parseArgs(args);
      if (env !== initialEnv) payload.env = parseEnv(env);
      const result = await editServer.mutateAsync({ id: server.id, input: payload });
      if (result.error) return;
    } else {
      const payload = {
        name,
        type,
        package: pkg,
        args: parseArgs(args),
        env: parseEnv(env),
        visibility,
      };
      const result = await addServer.mutateAsync(payload);
      if (result.error) return;
    }
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {!controlled ? (
        <DialogTrigger asChild>
          <Button>Add server</Button>
        </DialogTrigger>
      ) : null}
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{controlled ? "Edit server" : "Add server"}</DialogTitle>
        </DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit}>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="space-y-1">
              <Label>Type</Label>
              <Select value={type} onValueChange={(v) => setType(v as ServerType)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="pypi">PyPI (uvx)</SelectItem>
                  <SelectItem value="npm">npm (npx)</SelectItem>
                  <SelectItem value="git">Git repo</SelectItem>
                  <SelectItem value="cmd">Command</SelectItem>
                  <SelectItem value="proxy">Proxy (remote URL)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label>Visibility</Label>
              <Select
                value={visibility}
                onValueChange={(v) => setVisibility(v as ServerVisibility)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="private">Just me</SelectItem>
                  <SelectItem value="everyone">Everyone</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="package">Package / source</Label>
            <Input
              id="package"
              value={pkg}
              onChange={(e) => setPkg(e.target.value)}
              placeholder="mcp-server-fetch or git+https://... or /usr/bin/cmd or http://host:port/mcp"
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label htmlFor="args">Args (comma-separated)</Label>
              <Input id="args" value={args} onChange={(e) => setArgs(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="env">Env (KEY=VALUE, comma-separated)</Label>
              <Input id="env" value={env} onChange={(e) => setEnv(e.target.value)} />
            </div>
          </div>
          {type === "proxy" ? (
            <p className="text-xs text-muted-foreground">
              Args and env are ignored for the proxy type — it connects to an
              already-running server, nothing gets launched locally.
            </p>
          ) : null}
          {mutation.isError ? (
            <p className="text-sm text-destructive">{mutation.error.message}</p>
          ) : null}
          {mutation.data?.error ? (
            <p className="text-sm text-destructive">
              {controlled ? "Saved, but failed to restart" : "Started with error"}:{" "}
              {mutation.data.error}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending
                ? controlled
                  ? "Saving…"
                  : "Installing…"
                : controlled
                  ? "Save changes"
                  : "Add server"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
