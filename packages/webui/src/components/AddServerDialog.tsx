import { useState } from "react";
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
import { useAddServer } from "@/hooks/useServers";
import type { ServerType } from "@/lib/types";

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

export function AddServerDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<ServerType>("pypi");
  const [pkg, setPkg] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const addServer = useAddServer();

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    await addServer.mutateAsync({
      name,
      type,
      package: pkg,
      args: parseArgs(args),
      env: parseEnv(env),
    });
    setOpen(false);
    setName("");
    setPkg("");
    setArgs("");
    setEnv("");
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>Add server</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add server</DialogTitle>
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
              placeholder="mcp-server-fetch or git+https://... or /usr/bin/cmd"
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
          {addServer.isError ? (
            <p className="text-sm text-destructive">{addServer.error.message}</p>
          ) : null}
          {addServer.data?.error ? (
            <p className="text-sm text-destructive">
              Started with error: {addServer.data.error}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="submit" disabled={addServer.isPending}>
              {addServer.isPending ? "Installing…" : "Add server"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
