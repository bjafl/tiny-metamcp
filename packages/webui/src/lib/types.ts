export type ServerType = "pypi" | "npm" | "git" | "cmd" | "proxy";
export type ServerVisibility = "everyone" | "private";

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
  owner: string | null;
  visibility: ServerVisibility;
}

export interface AddServerInput {
  name: string;
  type: ServerType;
  package: string;
  args: string[];
  env: Record<string, string>;
  visibility: ServerVisibility;
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

export interface UserIdentitySummary {
  id: number;
  provider: string;
  raw_id: string;
  display_name: string | null;
}

export interface Me {
  username: string;
  is_admin: boolean;
  display_name: string | null;
  identities: UserIdentitySummary[];
}

export type AuthProviders = Record<string, boolean>;

export interface User {
  id: number;
  is_admin: boolean;
  allowed: boolean;
  created_at: number;
  identities: UserIdentitySummary[];
}

export interface UpdateUserInput {
  is_admin?: boolean;
  allowed?: boolean;
}

export interface AllowedIdentity {
  id: number;
  provider: string;
  raw_id: string;
  grant_admin: boolean;
}

export interface AddAllowedIdentityInput {
  provider: string;
  raw_id: string;
  grant_admin: boolean;
}

export interface GenerateTokenResult {
  token: string;
}
