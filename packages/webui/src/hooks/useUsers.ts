import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AddAllowedIdentityInput, UpdateUserInput } from "@/lib/types";

const usersKey = ["users"] as const;
const allowedIdentitiesKey = ["allowed-identities"] as const;

export function useUsers() {
  return useQuery({ queryKey: usersKey, queryFn: api.listUsers });
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: number; input: UpdateUserInput }) =>
      api.updateUser(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: usersKey }),
  });
}

export function useAllowedIdentities() {
  return useQuery({ queryKey: allowedIdentitiesKey, queryFn: api.listAllowedIdentities });
}

export function useAddAllowedIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: AddAllowedIdentityInput) => api.addAllowedIdentity(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: allowedIdentitiesKey }),
  });
}

export function useDeleteAllowedIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteAllowedIdentity(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: allowedIdentitiesKey }),
  });
}
