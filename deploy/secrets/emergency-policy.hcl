# Independent authentication permits a ceremony; the unseal quorum authorizes root.
path "sys/generate-root-token/attempt" {
  capabilities = ["read", "update", "delete"]
}
path "sys/generate-root-token/update" {
  capabilities = ["update"]
}
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
