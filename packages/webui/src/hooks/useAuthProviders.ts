import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useAuthProviders() {
  return useQuery({ queryKey: ["auth-providers"], queryFn: api.authProviders });
}
