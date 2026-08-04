import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AddServerInput } from "@/lib/types";

const serversKey = ["servers"] as const;

export function useServers() {
  return useQuery({
    queryKey: serversKey,
    queryFn: api.listServers,
    refetchInterval: 5000,
  });
}

export function useAddServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: AddServerInput) => api.addServer(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useEditServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: number; input: Partial<AddServerInput> }) =>
      api.editServer(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useDeleteServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useEnableServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.enableServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useDisableServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.disableServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useRestartServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.restartServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}
