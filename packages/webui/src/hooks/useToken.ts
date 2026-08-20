import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useGenerateToken() {
  return useMutation({ mutationFn: api.generateToken });
}
