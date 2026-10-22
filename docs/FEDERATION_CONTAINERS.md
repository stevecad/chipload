# Running federation containers

Chipload instances communicate directly over HTTPS or through an optional cloud
relay. The directory lists published offerings and their disclosed locations;
a candidate still needs a fresh evaluation of technical and business conditions.
A material seller, material processor and a shop with observed stock are separate
contracts. Search never implies qualification, current stock or a purchase.

Build the images from the same checkout before preparing deployments:

```sh
./scripts/build-worker
./scripts/build-runtime
./scripts/build-tls
```

The runtime build produces `api`, `broker`, `executor`, `cloud` and `connector`
images and records their immutable IDs under `.local/`. The launcher checks the
SDK, lock and Dockerfile recipe fingerprints and pins the selected IDs. Use a
non-root Linux account with a local Unix Docker socket. Configuration and
credential files must be owner-only; keep state directories at mode 0700.

## Cloud directory and relay

Prepare a `CloudSettings` JSON file using the contract in
[FEDERATION.md](FEDERATION.md#implemented-cloud-service). Set a durable random
`directory_id`, Keycloak issuer, exact cloud `/mcp` audience and pinned JWKS trust.
Each enrolled instance needs a separate `OwnerBinding` with its verified
service-account subject, durable instance ID, and explicit advertise/connect
permissions. An API shop grant does not enroll an instance in the cloud.

Supply `certificates/api.crt` and `certificates/api.key` under the TLS material
directory. The certificate must contain the configured cloud hostname and its
chain. Keep the private key outside source control. The launcher verifies the
certificate/key pair, hostname and validity. The cloud uses the resource URL's
host and port as its TLS listener, bound to loopback by default:

```sh
uv run --extra server python scripts/federation prepare \
  --state /absolute/private/cloud --role cloud --name chipload-cloud \
  --config /absolute/private/cloud-input.json \
  --certificates /absolute/private/cloud-tls
uv run --extra server python scripts/federation initialize --state /absolute/private/cloud
uv run --extra server python scripts/federation up --state /absolute/private/cloud
```

Set `--listen-address` during preparation to choose another IPv4 client listener.
Use operator-managed DNS and certificates when exposing an endpoint. The gateway
owns the TLS private key; the backend receives only configuration, CA files and
its private data directory. The backend has no published host port. Separate
application, edge and egress networks keep frontend traffic separate from
outbound source-proof and JWKS requests. Proxy trust names the exact gateway IP.

Initialization is explicit and allowed once on fresh data. A durable marker
blocks startup after an uncertain initialization. `verify-initialization` checks
the existing directory and paired accounting stores before clearing the marker;
it never creates or migrates missing accounting state. Restore all three stores
from a consistent stopped backup if any is lost. Keep deployment settings and
instance-owner bindings with that backup. Do not initialize over a partial store.
Before reopening a restored cloud listener, reconcile current owner bindings
and credential revocations with the identity and secrets services. An older
snapshot can expose a withdrawn advertisement until its lease expires (at most
300 seconds). If post-backup withdrawal history is unavailable, keep the listener
stopped until all restored leases expire. Do not reopen while owner or credential
changes remain unresolved. This launcher has no automated restore or post-backup
reconciliation command.
Cloud relay sessions are ephemeral and reconnect after restart; old deliveries
are never replayed. This is a single-process, single-host cloud package.

## Outbound connector

Use `ConnectorSettings` from [CONNECTOR.md](CONNECTOR.md). A connector reads its
own source API's public projection. It can publish indexed offerings, poll the
relay, and dispatch caller-owned network reads. The source and each cloud have
separate stable identities and egress policies. A query-only cloud link needs no
enrollment credential. For local dispatch, configure a separate Keycloak client
with `shop:connect` and an explicit local `network:dispatch` grant. Ordinary shop
agents retain `network:query` without dispatch authority.

Provision separate OpenBao AppRoles for local dispatch and each cloud enrollment.
Each policy may read exactly its own KV-v2 client-secret record and revoke its
own token. Verify actual read/write/list denials when provisioning. Tokens must
have no default policy, no identity policies and a lifetime of at most five
minutes. Keycloak tokens must have the configured client ID, subject, issuer and
resource-URI audience. Store only `role-id`, `secret-id` and the configured CA file
in each private workload credential directory. Root tokens, unseal shares, PGP
keys and unrelated credentials are refused there.

```sh
uv run --extra server python scripts/federation prepare \
  --state /absolute/private/connector --role connector --name chipload-connector \
  --config /absolute/private/connector-input.json \
  --network chipload-tls_application --network chipload-secrets
uv run --extra server python scripts/federation up --state /absolute/private/connector
```

External network joins are explicit operator inputs, capped at four. Join only
the networks needed for the source API, identity service, cloud and OpenBao.
The connector has no listener, shop database mount or Docker socket. Configuration,
CA bundles and credential directories are mounted read-only. Shop source and
hosted Python containers receive none of these mounts and retain network
isolation. All federation service containers run non-root with read-only roots,
dropped capabilities, no-new-privileges, process/CPU/memory limits and bounded logs.

Default egress permits public HTTPS on port 443. Exact `private_origins` allow
operator-approved LAN endpoints. For a gateway whose container listens on a
different port, an operator can add `routes: [{"origin": "https://shop.example",
"port": 8443}]` to its policy. A route requires that same origin in
`private_origins`. Only the socket's destination port changes: TLS SNI,
certificate verification, HTTP Host, OAuth audience and peer identity continue
to use the original URL. Routes cannot be supplied by shop Python or a network
job. Link-local metadata and other prohibited address classes remain blocked.

## Operations and the reproducible drill

`status`, `logs` and `down` accept the same `--state`. Stopping preserves data and
works even when credentials are unavailable. Prepared settings are immutable;
configuration or image changes require a reviewed replacement deployment using
the retained data and matching launcher recipe. Do not delete initialized cloud
state to upgrade it. After replacing mounted CA/certificate files or rotating a
workload SecretID atomically, use `up` to recreate mounts. Renew certificates and
rotate credentials under operator custody before their expiry. Automatic cloud
certificate renewal, multi-host recovery and high availability are not supplied
by this launcher.

The full disposable drill builds and publishes three ordinary Python shops,
starts their real Keycloak instances and TLS gateways, initializes TLS OpenBao
with encrypted threshold bootstrap, provisions narrow AppRoles, and retires the
bootstrap root token. Run it with the built OpenBao image as well:

```sh
./scripts/build-secrets
uv run --extra server python scripts/check-live federation
```

The drill searches for exact aluminum grade/condition/form near a milling shop.
The closest supplier rejects a five-piece request against its 100-piece minimum;
the farther supplier accepts through an outbound relay. Conventional REST and
hosted Python through the four MCP tools exercise the same services. It checks
direct/relay outcomes, private supplier topology, actual OpenBao ACL denials,
container authority, withdrawal, lease expiry, and direct dispatch after cloud
outage and connector restart. All listeners bind to loopback; test-only private
CAs, encrypted bootstrap material, networks and data are disposable. Cleanup
removes only fixture-owned deployments and volumes. This drill qualifies the
portable workflow; an operator still needs site-specific DNS, certificate
lifecycle, recovery custody and capacity qualification before a public launch.
