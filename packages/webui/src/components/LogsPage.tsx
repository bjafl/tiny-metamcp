import { useEffect, useMemo, useRef, useState } from "react";
import { useServers } from "@/hooks/useServers";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import type { LogEntry } from "@/lib/types";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
const LEVEL_COLOR: Record<string, string> = {
  DEBUG: "text-slate-400",
  INFO: "text-emerald-400",
  WARNING: "text-amber-300",
  ERROR: "text-rose-400",
};
const ALL = "__all__";

export function LogsPage() {
  const { data: servers } = useServers();
  const [serverFilter, setServerFilter] = useState<string>(ALL);
  const [levelFilter, setLevelFilter] = useState<string>(ALL);
  const [lines, setLines] = useState<LogEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    const url =
      serverFilter === ALL
        ? "/api/logs/stream"
        : `/api/logs/stream?server=${encodeURIComponent(serverFilter)}`;
    const es = new EventSource(url);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e) => {
      const entry = JSON.parse(e.data) as LogEntry;
      setLines((prev) => (prev.length >= 2000 ? [...prev.slice(1), entry] : [...prev, entry]));
    };
    return () => es.close();
  }, [serverFilter]);

  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  const filtered = useMemo(
    () => lines.filter((l) => levelFilter === ALL || l.level === levelFilter),
    [lines, levelFilter],
  );

  const runningServers = (servers ?? []).filter((s) => s.running).map((s) => s.name);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-48">
            <SelectValue placeholder="All servers" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All servers</SelectItem>
            {runningServers.map((name) => (
              <SelectItem key={name} value={name}>
                {name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={levelFilter} onValueChange={setLevelFilter}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="All levels" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All levels</SelectItem>
            {LEVELS.map((lvl) => (
              <SelectItem key={lvl} value={lvl}>
                {lvl}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button variant="outline" size="sm" onClick={() => setLines([])}>
          Clear
        </Button>
        <span className={connected ? "text-sm text-emerald-500" : "text-sm text-destructive"}>
          {connected ? "● Live" : "○ Disconnected"}
        </span>
      </div>
      <div
        ref={boxRef}
        className="h-96 overflow-y-auto rounded-md bg-slate-950 p-3 font-mono text-xs"
      >
        {filtered.length === 0 ? (
          <span className="text-slate-500">No log entries yet.</span>
        ) : null}
        {filtered.map((l, i) => (
          <div key={`${l.ts}-${i}`} className="flex gap-2">
            <span className="min-w-[8ch] text-slate-500">
              {new Date(l.ts * 1000).toTimeString().slice(0, 8)}
            </span>
            <span className={`min-w-[7ch] font-semibold ${LEVEL_COLOR[l.level] ?? ""}`}>
              {l.level}
            </span>
            <span className="min-w-[10ch] text-sky-400">{l.server || "-"}</span>
            <span className="break-all text-slate-200">{l.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
