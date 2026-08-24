import { useState } from "react";
import type { FormEvent } from "react";
import { useMe } from "@/hooks/useMe";
import {
  useAddAllowedIdentity,
  useAllowedIdentities,
  useDeleteAllowedIdentity,
  useUpdateUser,
  useUsers,
} from "@/hooks/useUsers";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function UsersPage() {
  const { data: me } = useMe();

  if (!me?.is_admin) {
    return <p className="text-sm text-muted-foreground">Admins only.</p>;
  }

  return (
    <div className="space-y-8">
      <UsersTable currentUserId={idFromUsername(me.username)} />
      <AllowedIdentitiesSection />
    </div>
  );
}

function idFromUsername(username: string): number {
  return Number(username.replace("user:", ""));
}

function UsersTable({ currentUserId }: { currentUserId: number }) {
  const { data: users } = useUsers();
  const updateUser = useUpdateUser();

  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">Users</h2>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Identities</TableHead>
            <TableHead>Admin</TableHead>
            <TableHead>Allowed</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(users ?? []).map((user) => {
            const isSelf = user.id === currentUserId;
            return (
              <TableRow key={user.id}>
                <TableCell className="space-x-1">
                  {user.identities.map((identity) => (
                    <Badge key={identity.id} variant="outline">
                      {identity.provider}:{identity.display_name ?? identity.raw_id}
                    </Badge>
                  ))}
                </TableCell>
                <TableCell>{user.is_admin ? "Yes" : "No"}</TableCell>
                <TableCell>{user.allowed ? "Yes" : "No"}</TableCell>
                <TableCell className="space-x-2 text-right">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={(isSelf && user.is_admin) || updateUser.isPending}
                    onClick={() =>
                      updateUser.mutate({ id: user.id, input: { is_admin: !user.is_admin } })
                    }
                  >
                    {user.is_admin ? "Revoke admin" : "Make admin"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={(isSelf && user.allowed) || updateUser.isPending}
                    onClick={() =>
                      updateUser.mutate({ id: user.id, input: { allowed: !user.allowed } })
                    }
                  >
                    {user.allowed ? "Disable" : "Enable"}
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      {updateUser.isError ? (
        <p className="text-sm text-destructive">{updateUser.error.message}</p>
      ) : null}
    </div>
  );
}

function AllowedIdentitiesSection() {
  const { data: rows } = useAllowedIdentities();
  const addRow = useAddAllowedIdentity();
  const deleteRow = useDeleteAllowedIdentity();
  const [provider, setProvider] = useState("github");
  const [rawId, setRawId] = useState("");
  const [grantAdmin, setGrantAdmin] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    await addRow.mutateAsync({ provider, raw_id: rawId, grant_admin: grantAdmin });
    setRawId("");
    setGrantAdmin(false);
  }

  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">Pending identities</h2>
      <p className="text-sm text-muted-foreground">
        Pre-approve a raw GitHub login or SteamID64 before that person has logged in.
        Leave both allow-lists empty for a provider to let anyone with that provider sign
        in.
      </p>
      <form className="flex items-end gap-2" onSubmit={handleSubmit}>
        <div className="space-y-1">
          <Label>Provider</Label>
          <Select value={provider} onValueChange={setProvider}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="github">GitHub</SelectItem>
              <SelectItem value="steam">Steam</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="raw-id">Raw ID</Label>
          <Input
            id="raw-id"
            value={rawId}
            onChange={(e) => setRawId(e.target.value)}
            placeholder="octocat or 76561198012345678"
            required
          />
        </div>
        <label className="flex items-center gap-1 pb-2 text-sm">
          <input
            type="checkbox"
            checked={grantAdmin}
            onChange={(e) => setGrantAdmin(e.target.checked)}
          />
          Grant admin
        </label>
        <Button type="submit" disabled={addRow.isPending}>
          Add
        </Button>
      </form>
      {addRow.isError ? <p className="text-sm text-destructive">{addRow.error.message}</p> : null}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Provider</TableHead>
            <TableHead>Raw ID</TableHead>
            <TableHead>Grants admin</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(rows ?? []).map((row) => (
            <TableRow key={row.id}>
              <TableCell>{row.provider}</TableCell>
              <TableCell>{row.raw_id}</TableCell>
              <TableCell>{row.grant_admin ? "Yes" : "No"}</TableCell>
              <TableCell className="text-right">
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={deleteRow.isPending}
                  onClick={() => deleteRow.mutate(row.id)}
                >
                  Remove
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
