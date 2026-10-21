# Outbound shop connector

`chipload.connector` is a trusted process for a shop's instance. It connects to
up to four cloud directories, publishes selected public listings and carries
public discovery/evaluation queries back to the shop over outbound HTTPS. It
executes no shop Python and needs neither the shop database nor Docker access.
Hosted Python uses the [caller-owned network queue](NETWORK.md). Federation
container packaging is described in [FEDERATION_CONTAINERS.md](FEDERATION_CONTAINERS.md).

## Configuration and startup

Enable the local API's stable `instance_id` first. The connector verifies that
ID at its configured `source_origin` before enrolling with a cloud. The source
can be a private HTTPS listener: public inbound access is optional. Its source
client uses only public peer queries and sends no bearer token to the shop.

Publishing or receiving relayed queries requires an operator-created
instance-owner binding at that cloud. Public outbound searches and relayed
queries need no cloud credential; see [NETWORK.md](NETWORK.md). Provision a
separate Keycloak service client with the cloud resource audience and record
its service-account subject in that binding. Give it only the required cloud
`advertise`/`connect` permissions; local shop grants do not confer cloud grants.
The connector verifies the cloud directory ID before sending an enrollment token.

Store that client's secret in a single OpenBao KV-v2 field. Provision an AppRole
with no default policy, a token lifetime of at most 300 seconds, and a policy
allowing only that exact secret read and self-revocation. Verify the actual
policy contents and access denials when provisioning it; matching a policy name
in a login response does not establish its ACL contents. The connector needs
only `role-id`, `secret-id` and an optional named OpenBao CA file in its private
credential directory. All must be owner-only regular files; the directory must
also be owner-only. Operator and administrative credentials stay outside it.

A configuration file has this shape (generate and preserve your own instance
and directory IDs, and substitute your provisioned endpoints and subject):

```json
{
  "instance_id": "11111111111111111111111111111111",
  "source_origin": "https://shop.internal",
  "source_policy": {
    "private_origins": ["https://shop.internal"],
    "ca_file": "/etc/chipload/source-ca.pem",
    "seconds": 10
  },
  "public_origin": null,
  "shops": ["shop"],
  "renew_seconds": 30,
  "lease_seconds": 120,
  "clouds": [{
    "id": "suppliers",
    "origin": "https://directory.example",
    "directory_id": "22222222222222222222222222222222",
    "credentials": "/run/chipload/cloud-credentials",
    "advertise": true,
    "relay": true,
    "identity": {
      "client_id": "connector-cloud",
      "subject": "provisioned-keycloak-service-account-subject",
      "trust": {
        "resource_url": "https://directory.example/mcp",
        "issuer": "https://identity.example/realms/chipload",
        "jwks_url": "https://identity.example/realms/chipload/protocol/openid-connect/certs"
      },
      "secrets": {
        "origin": "https://bao.internal",
        "ca_file": "bao-ca.pem",
        "kv_mount": "connector",
        "secret_path": "cloud/client",
        "field": "client-secret",
        "policy": "connector-cloud"
      },
      "policy": {
        "private_origins": ["https://bao.internal"],
        "seconds": 15
      }
    }
  }]
}
```

`identity.policy` controls the cloud, issuer and JWKS HTTPS clients and the
OpenBao destination policy. The optional OpenBao CA file overrides CA trust only
for its requests. Other private issuer/cloud origins need explicit exceptions
and, if necessary, a CA bundle containing their trust roots. Metadata/link-local
addresses remain blocked. Credentials are sent only to their configured service;
the connector adds no redirect, proxy or arbitrary-header facility.

Keep configuration owner-only and run under the account owning the mounted
credentials:

```sh
uv run --extra server python -m chipload.connector --config /absolute/private/connector.json
```

There is no connector listener. SIGINT/SIGTERM stop admission, close known
sessions and wait for bounded in-flight reads before clearing cached tokens.
A cloud outage can prevent confirmed session closure; session leases expire.
Warnings use operation codes and configured cloud/shop IDs, excluding tokens,
secret values and query bodies.

## Publication and relay behavior

The operator's `shops` list bounds publication authority; the shop's Python
project chooses its public, indexed listings, exact semantic mappings and site
coordinates. Set `public_origin` to the shop's externally reachable HTTPS origin
for direct advertisements, or leave it null and enable relay. Direct-only
publication can disable relay. A relay-only connector can disable advertising
and omit the shop list.

The default renewal interval is 30 seconds and lease is 120 seconds; leases must
cover at least three configured renewal intervals. Each refresh reads the current
public source catalog and cloud head, then submits a fresh observation with a CAS
version. Only explicitly indexed listings are advertised. A local withdrawal or
removal of every indexed listing withdraws the directory entry. Unavailable
source/cloud services do not turn failures into domain decisions; old entries
expire if refresh cannot finish. An interrupted write is reconciled by reading
the cloud head on the next refresh, rather than resending an old advertisement.

Relay-only publication waits for an outbound session so the cloud can verify the
source catalog through it. The connector maintains one poll per cloud with
receiving enabled and at most four simultaneous delivered reads across clouds
and local dispatch. Publication refreshes have their own bounded per-cloud loop.
It preserves each
delivery's request ID, instance, publication guards and deadline. It forwards
only the four fixed peer actions, reports oversized replies as transport failures,
and never replays a lost delivery. Session replacement fences replies from an
older connection. Reconnection uses a stoppable backoff capped at 30 seconds.
Direct queries continue independently of cloud connectivity.

## Credential lifetime and verification

`connector_auth.py` reads the configured OpenBao field through a short-lived
AppRole token, then attempts self-revocation. It submits the client secret to the
fixed Keycloak token endpoint using a form body, never a URL. All network calls,
including JWKS reads, use the bounded transport with address pinning and TLS name
verification. The full refresh has a 30-second budget with bounded child cleanup.
Secret echo, duplicate JSON, encoded responses, redirects and oversized form
expansion are rejected.

The shared JWT verifier checks signature, issuer, audience and lifetime. The
provider additionally checks the service-account subject and authorized client.
It keeps only the verified bearer token in memory, refreshes before expiry and
refetches the OpenBao field on refresh. A failed refresh has no cached fallback;
401 responses invalidate the token. Atomic credential-file replacement and KV
secret rotation take effect on the next refresh. Rotating a client secret does
not revoke already-issued JWTs; existing server lifetime checks permit at most
15 minutes, and the cloud still verifies every request.

## Verification and remaining integration

```sh
uv run --extra server python -m unittest discover -s tests -p 'test_connector*.py' -v
uv run --extra server python -m unittest discover -s tests -p test_federation_wire.py -v
```

Actual TLS protocol fixtures cover credential rotation, wrong token identities,
over-broad/long-lived workload tokens, denied or destroyed secret versions,
secret echo and form expansion. Managed connector checks cover automatic
relay-only publication, direct-only publication, current listing evaluation,
withdrawal, reconnect after cloud restart, direct reads during outage and source
identity mismatch before enrollment. These use signed fixture tokens for the
managed loop. Hosted Python has a separate actual-container check in
`test_network_live.py`. The combined `test_federation_live.py` drill uses real
Keycloak and OpenBao, isolated source builds, REST/MCP supplier selection,
withdrawal, lease expiry and direct dispatch during cloud outage. Run the
`federation` live profile using [the container instructions](FEDERATION_CONTAINERS.md).
Site-specific public deployment remains a separate qualification.
