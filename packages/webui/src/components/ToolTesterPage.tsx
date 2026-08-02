import { useMemo, useState } from "react";
import { useCallTool, useTools } from "@/hooks/useTools";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export function ToolTesterPage() {
  const { data: tools } = useTools();
  const callTool = useCallTool();
  const [server, setServer] = useState("");
  const [tool, setTool] = useState("");
  const [args, setArgs] = useState("{}");

  const servers = useMemo(() => [...new Set((tools ?? []).map((t) => t.server))], [tools]);
  const toolsForServer = useMemo(
    () => (tools ?? []).filter((t) => t.server === server),
    [tools, server],
  );
  const schema = toolsForServer.find((t) => t.tool === tool)?.inputSchema;

  function handleCall() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(args);
    } catch {
      alert("Invalid JSON in arguments");
      return;
    }
    callTool.mutate({ server, tool, arguments: parsed });
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <Select
          value={server}
          onValueChange={(v) => {
            setServer(v);
            setTool("");
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="Select server" />
          </SelectTrigger>
          <SelectContent>
            {servers.map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={tool} onValueChange={setTool}>
          <SelectTrigger>
            <SelectValue placeholder="Select tool" />
          </SelectTrigger>
          <SelectContent>
            {toolsForServer.map((t) => (
              <SelectItem key={t.tool} value={t.tool}>
                {t.tool}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {schema ? (
        <div>
          <p className="mb-1 text-sm text-muted-foreground">Input schema:</p>
          <pre className="max-h-48 overflow-y-auto rounded-md border bg-muted p-2 text-xs">
            {JSON.stringify(schema, null, 2)}
          </pre>
        </div>
      ) : null}
      <div className="space-y-1">
        <label className="text-sm font-medium">Arguments (JSON)</label>
        <Textarea
          rows={4}
          className="font-mono text-sm"
          value={args}
          onChange={(e) => setArgs(e.target.value)}
        />
      </div>
      <Button onClick={handleCall} disabled={!tool || callTool.isPending}>
        {callTool.isPending ? "Calling…" : "Call tool"}
      </Button>
      {callTool.data ? (
        <div>
          <p className="mb-1 text-sm text-muted-foreground">Result:</p>
          <pre
            className={`max-h-64 overflow-y-auto rounded-md border p-2 text-xs ${
              callTool.data.isError ? "border-destructive bg-destructive/10" : "bg-muted"
            }`}
          >
            {JSON.stringify(callTool.data.content, null, 2)}
          </pre>
        </div>
      ) : null}
      {callTool.isError ? (
        <p className="text-sm text-destructive">{callTool.error.message}</p>
      ) : null}
    </div>
  );
}
