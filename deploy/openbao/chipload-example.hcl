# A workload may read this example shop's credential values, with no writes,
# listing, other-shop access, or access to OpenBao administration.
path "chipload/data/shops/example/*" {
  capabilities = ["read"]
}

# Workloads manage their own short-lived tokens without the broad default policy.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
