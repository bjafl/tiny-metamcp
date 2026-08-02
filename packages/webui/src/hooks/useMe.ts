import { queryOptions, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const meQueryOptions = queryOptions({
  queryKey: ["me"],
  queryFn: api.me,
  retry: false,
});

export function useMe() {
  return useQuery(meQueryOptions);
}
