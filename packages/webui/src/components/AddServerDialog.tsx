import { useEffect, useState } from "react";
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
import type { ServerConfig, ServerType } from "@/lib/types";

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

interface AddServerDialogProps {
  server?: ServerConfig;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

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
  const [pkg, setPkg] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const addServer = useAddServer();
  const editServer = useEditServer();
  const mutation = controlled ? editServer : addServer;

  useEffect(() => {
    if (!open) return;
    editServer.reset();
    setName(server?.name ?? "");
    setType(server?.type ?? "pypi");
    setPkg(server?.package ?? "");
    setArgs(server ? formatArgs(server.args) : "");
    setEnv(server ? formatEnv(server.env) : "");
  }, [open, server]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const payload = { name, type, package: pkg, args: parseArgs(args), env: parseEnv(env) };
    if (controlled && server) {
      await editServer.mutateAsync({ id: server.id, input: payload });
    } else {
      await addServer.mutateAsync(payload);
    }
    setOpen(false);
    if (!controlled) {
      setName("");
      setPkg("");
      setArgs("");
      setEnv("");
    }
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
              Started with error: {mutation.data.error}
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
