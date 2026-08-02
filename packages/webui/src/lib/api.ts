import type {
  AddServerInput,
  AddServerResult,
  CallToolInput,
  CallToolResult,
  Me,
  ServerConfig,
  ToolInfo,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response body wasn't JSON — keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  me: () => request<Me>("/api/me"),
  listServers: () => request<ServerConfig[]>("/api/servers"),
  addServer: (input: AddServerInput) =>
    request<AddServerResult>("/api/servers", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  deleteServer: (id: number) =>
    request<{ deleted: number }>(`/api/servers/${id}`, { method: "DELETE" }),
  enableServer: (id: number) =>
    request<{ id: number; enabled: true; tool_count: number }>(
      `/api/servers/${id}/enable`,
      { method: "POST" },
    ),
  disableServer: (id: number) =>
    request<{ id: number; enabled: false }>(`/api/servers/${id}/disable`, {
      method: "POST",
    }),
  restartServer: (id: number) =>
    request<{ id: number; tool_count: number }>(`/api/servers/${id}/restart`, {
      method: "POST",
    }),
  listTools: () => request<ToolInfo[]>("/api/tools"),
  callTool: (input: CallToolInput) =>
    request<CallToolResult>("/api/tools/call", {
      method: "POST",
      body: JSON.stringify(input),
    }),
};
