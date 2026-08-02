import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { CallToolInput } from "@/lib/types";

export function useTools() {
  return useQuery({ queryKey: ["tools"], queryFn: api.listTools });
}

export function useCallTool() {
  return useMutation({
    mutationFn: (input: CallToolInput) => api.callTool(input),
  });
}
