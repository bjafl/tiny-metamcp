import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const meQueryOptions = queryOptions({
  queryKey: ["me"],
  queryFn: api.me,
  retry: false,
});

export function useMe() {
  return useQuery(meQueryOptions);
}

export function useUnlinkIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.unlinkIdentity(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}
