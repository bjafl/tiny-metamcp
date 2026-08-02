export type ServerType = "pypi" | "npm" | "git" | "cmd";

export interface ServerConfig {
  id: number;
  name: string;
  type: ServerType;
  package: string;
  args: string[];
  env: Record<string, string>;
  enabled: boolean;
  running: boolean;
  tool_count: number;
  error: string | null;
}

export interface AddServerInput {
  name: string;
  type: ServerType;
  package: string;
  args: string[];
  env: Record<string, string>;
}

export interface AddServerResult {
  server: ServerConfig;
  tools: string[];
  error?: string;
}

export interface ToolInfo {
  server: string;
  tool: string;
  description: string | null;
  inputSchema: Record<string, unknown>;
}

export interface CallToolInput {
  server: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface CallToolResult {
  server: string;
  tool: string;
  content: unknown[];
  isError: boolean;
}

export interface LogEntry {
  ts: number;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  server: string;
  msg: string;
}

export interface Me {
  username: string;
}
