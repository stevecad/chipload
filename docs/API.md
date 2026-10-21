# REST and MCP

Chipload serves REST and Streamable HTTP MCP from one application. Both call the
same operation contracts, application services, permission checks, and published
revisions. Public discovery works without authentication or an agent.

The server defaults to a loopback listener and does not import shop source or
invoke Docker. A separate broker processes isolated build and Python jobs. Both can run in
[separate application containers](RUNTIME.md) with process and filesystem limits.
The [TLS stack](TLS.md) adds verified HTTPS, production-mode Keycloak, and private
networks. Public edge qualification, stronger isolation, recovery exercises,
and deployment load/security qualification remain on the roadmap.

## Start the services

```sh
uv sync --locked --extra server
./scripts/dev-stack up
./scripts/build-worker
cp deploy/api/settings.example.json .local/server.json
chmod 600 .local/server.json
uv run --extra server python -m chipload.server --config .local/server.json
```

In another terminal:

```sh
uv run python -m chipload.broker --database .local/data/chipload.db --settings .local/server.json
```

The example settings use `.local/data/chipload.db`. Keep this directory dedicated
to the paired database files and their journals; settings and secrets stay outside
it. The broker requires local Docker access and `.local/worker-image`; the server
requires neither. With `--settings`, each broker runs a Python loop alongside its build loop.
Python programs have separate capacity and can wait for a build. Each broker
recovers expired claims and processes one build at a time. Multiple brokers share atomic
dispatch and running-job limits in the database. Eligible shops rotate by their
last dispatch turn, with identity turns and queued time breaking ties. New
queues join at the current turn. Shutdown lets the current bounded job finish.
`--once` processes at most one eligible job, which is useful in tests.

`--bind` and `--port` configure the API listener independently of its public
resource URL. Container packaging uses `--bind 0.0.0.0 --port 8088` and publishes
that port only on host loopback. Host/Origin checks still use the resource URL;
general proxy-header rewriting remains disabled. The TLS stack keeps port 8088
private and configures the gateway trust described below. The launcher allows 15 seconds for
graceful request completion during shutdown.

Use the [local authoring walkthrough](AUTHORING.md) to publish an example before
trying anonymous discovery. Publication through that CLI and through the network
interfaces selects the same database record when they use the same database.
Pass `--database .local/data/chipload.db` to the walkthrough's `chipload.local`
commands for this setup. CLI defaults remain `.local/chipload.db`; existing data
is never moved automatically. See [Python deployment](PYTHON_API.md#deployment-and-upgrade)
before enabling hosted Python against an existing repository.

```sh
curl --fail http://localhost:8088/v1/shops/thermal-example/catalog
curl --fail 'http://localhost:8088/v1/shops/thermal-example/capabilities?limit=10'
curl --fail 'http://localhost:8088/v1/shops/thermal-example/processes?query=thermal'
curl --fail http://localhost:8088/openapi.json
python3 examples/discover.py http://localhost:8088 thermal-example
```

`/openapi.json` documents the REST schemas and bearer requirements. There is no
agent requirement for any REST operation. `python -m chipload.server --help`
documents the launcher; the settings example contains no identities or grants.

## Discovery and evaluation

| REST endpoint | Python client / MCP discovery |
| --- | --- |
| `GET /v1/sdk` | `client.reference()` / `chipload://sdk/reference` |
| `GET /v1/identity` (authenticated) | `client.identity()` / `chipload://identity/current` |
| `GET /v1/shops/{shop}/catalog` | `shop.catalog() / discover_shop: catalog` |
| `GET /v1/shops/{shop}/{collection}` | `shop.page() / discover_shop: page` |
| `GET /v1/shops/{shop}/{collection}/{id}` | `shop.inspect() / discover_shop: inspect` |
| `POST /v1/shops/{shop}/evaluate` | `shop.evaluate() / discover_shop: evaluate` |
| `POST /v1/shops/{shop}/variables/query` | `shop.variables() / discover_shop: variables` |

Collections are `sites`, `resources`, `catalogs`, `processes`, `capabilities`,
`workflows`, `offers`, `variables`, and `operations`. Only explicitly public records appear. Private and
missing IDs both return 404. Page sizes are 1–100; `query` filters names and IDs.
Follow the returned `next_cursor` with the same collection and filter. Cursors
are tied to the public revision; a changed revision returns 409 so the client
can restart pagination.

Catalog, list, and item responses have representation ETags. Use `If-None-Match`
to get 304 for an unchanged representation. Responses require revalidation.
Publication withdrawal is checked before conditional response handling, so an
old ETag cannot keep a withdrawn shop accessible. Other responses use `no-store`.

For example, query a shop policy without an agent:

```sh
curl --fail http://localhost:8088/v1/shops/thermal-example/variables/query \
  -H 'Content-Type: application/json' \
  --data '{"shop_id":"thermal-example","query":{"ids":["customer-qualification"]}}'
```

The example's unknown qualification remains null. An evaluation body has the
same `shop_id` envelope and a `query` matching the SDK's `EvaluationRequest`:
target kind and ID, named facts, initial state, purpose, minimum evidence level,
and whether simulation is permitted. The body shop ID must match the path.
Target kinds are `capability`, `workflow`, and `offer`. Offers assess alternative
routes under shared conditions; see [the model contract](SHOP_MODEL.md#alternative-routes).

Capabilities may publish metadata for [Python constraints](SHOP_MODEL.md#python-constraints).
Their entrypoints and source remain private. Use [queued assessments](ASSESSMENTS.md)
to execute them for a new query through REST or the Python SDK.
The synchronous data API reports `python-check-required` when answers are absent,
and does not substitute a passing build test for the current request.

The service supplies the evaluation time and stored observation snapshot.
Clients cannot inject a clock, observations, or a principal into an evaluation.
Simulation defaults to disallowed. An author can inspect the full result using
the separately authorized private evaluation operation.

Public evaluation returns an outcome, safe reason codes/explanations, public
revision, evaluation time, simulation flag, and freshness summaries. Publication
of a capability opts into disclosure of that capability's outcome and relevant
freshness summaries. Private equipment IDs, evidence IDs, observation IDs,
values, internal paths, model/build digests, and step traces are excluded.
Public capability contracts include input/output states and declared requirements.
An offer response also includes the public `alternatives` assessments and,
when supported or conditional, a `matching_alternative`. These references are
assessment results; callable operations retain their separate authorization.

If a required input catalog contains hidden allowed values, public evaluation
returns unknown without testing guessed values against the hidden set. Public
workflows with private steps also remain unknown. Publication does not turn
partial disclosure into proof of support.
This rule also covers offers with hidden alternatives or hidden conditions in
any nested workflow. Their public response contains no alternative decisions.

## Authenticated operations

MCP programs compose these calls through the [Python client](PYTHON_API.md).

| REST endpoint | Python client method |
| --- | --- |
| `GET /v1/shops/{shop}/source?revision=...` | `shop.source` |
| `PUT /v1/shops/{shop}/source` | `shop.save` |
| `POST /v1/shops/{shop}/builds` | `shop.build` |
| `GET /v1/shops/{shop}/builds/{build}` | `shop.build_result` |
| `GET /v1/shops/{shop}/publication` | `shop.publication` |
| `PUT /v1/shops/{shop}/publication` | `shop.publish / rollback / withdraw` |
| `GET /v1/shops/{shop}/audit?after=0&limit=100` | `shop.audit` |
| `GET /v1/shops/{shop}/evidence` | `shop.evidence` |
| `GET /v1/shops/{shop}/evidence/{evidence}/revocation` | `shop.evidence_revocation` |
| `PUT /v1/shops/{shop}/evidence/{evidence}/revocation` | `shop.revoke_evidence` |
| `GET /v1/shops/{shop}/observations` | `shop.observations` |
| `PUT /v1/shops/{shop}/observations` | `shop.set_observations` |
| `POST /v1/shops/{shop}/private/evaluate` | `shop.evaluate(private=True)` |
| `POST /v1/shops/{shop}/private/variables/query` | `shop.variables(private=True)` |
| `GET /v1/shops/{shop}/private/operations/{operation}` | `shop.operation` |
| `POST /v1/shops/{shop}/operations/{operation}/executions` | `shop.execute` |
| `GET /v1/shops/{shop}/operations/{operation}/executions/{execution}` | `shop.execution` |
| `POST /v1/shops/{shop}/operations/{operation}/executions/{execution}/cancel` | `shop.cancel` |

Callable operations use typed inputs/outputs, explicit simulation/live intent,
shop and operation grants, and separately provisioned adapter bindings. See
[OPERATIONS.md](OPERATIONS.md) for payloads, state transitions, quotas, durable
request keys, cancellation, and reconciliation. Public operation discovery does
not enable execution. The supplied executor supports simulated adapters and a separately configured
[HTTPS receiver with scoped credentials](ADAPTERS.md).

Source writes contain `project` and a required `expected_head` (null for a new
shop). Build requests contain `shop_id`, an exact source `revision`, and a
`request_key`. Retrying the same key, identity, shop, and revision returns the
same build; reusing the key for another revision returns 409. The response is
202 and the queued job is polled through build inspection.

Publication changes contain `shop_id`, `action` (`publish`, `rollback`, or
`withdraw`), `expected_generation`, and `build_id` for publish/rollback.
Publication still requires a successful build with acceptance cases. A failed
build cannot replace the current publication.

Observation writes replace the complete snapshot using `expected_revision`.
IDs must be unique and resources must exist in the published shop. The
observation's source, timestamp, and freshness are preserved; future, stale, or
simulated observations do not silently establish current physical support.
An optional `external` object carries `system` (ID), `record` (nonblank, at most
512 characters), and `revision` (null or nonblank, at most 200 characters).
These opaque references are returned only with authorized observation snapshots;
public catalog/evaluation projections omit them. Complete serialized snapshots
must fit the shared JSON limits. See the [ERP integration contract](ERP_INTEGRATION.md)
for preserving source time and merging updates after revision conflicts.
Required permissions are `observations:write`, `observations:read`,
`evidence:read`, and `evaluation:read` for the corresponding private operations.
Evidence revocation requires the separate `evidence:revoke` action and the
`shop:admin` role ceiling. Existing explicit bindings gain no new action.
Authoring permissions are listed in [AUTHORING.md](AUTHORING.md).

## Evidence revocation

The shop's agent can withdraw an evidence claim without rebuilding the shop:

```python
proofs = shop.evidence()
receipt = shop.revoke_evidence(
    "thermal-declaration",
    expected_model_sha256=proofs["revision"],
    reason="Qualification withdrawn after review",
)
```

The REST `PUT` body includes `shop_id`, `evidence_id`,
`expected_model_sha256` and `reason`. Path and body identities must match.
The service checks the currently published model, records the actor and time,
and writes an `evidence.revoked` audit event in the same transaction. A stale
model returns 409; an absent published evidence ID returns 404. The reason is
1–1,000 characters. Each shop can retain up to 10,000 revocations; exhaustion
returns `quota_exceeded` and never discards old revocations.

The evidence ID stays revoked within this repository, across source revisions,
edited declarations, publication withdrawal, and rollback. There is no
unrevocation operation. New qualification uses a new evidence ID. Retrying
the same actor, model and reason returns the original receipt, even after
withdrawal; a changed request for that ID returns 409. The authenticated `GET`
returns that receipt without requiring a current publication.

`shop.evidence()` includes `revocations` summaries for IDs in the published
model and a private `revocation_revision`. Use `shop.evidence_revocation(id)`
for the full private receipt. Private evaluation also identifies its revocation
snapshot. Public queries disclose only the usual safe evaluation results;
they expose neither revocation IDs, notes, actors nor snapshot hashes. Catalog
cache identities remain unchanged because evaluation is always current.

Operation admission and the dispatch check exclude revoked evidence. Work that
has already passed the dispatch check is not automatically cancelled or undone.
Revocations present in a recovery snapshot are preserved; restoring an older
snapshot requires reconciling later revocations before reopening service.

## Keycloak identity and grants

Configure a Keycloak client using authorization code with PKCE for interactive
clients, or an explicitly provisioned service account for a shop agent. Token
audience must contain the **exact `resource_url`**, including `/mcp`. This is also
the protected-resource identifier for REST in this deployment. The `chipload`
client ID alone is insufficient as an audience.

The development realm template adds `http://localhost:8088/mcp` as an access-token
audience on `chipload-cli`, alongside its existing client audience and role
mapper. Existing realms are not overwritten on import: add the corresponding
audience mapper to an existing client if it predates this template. Deployment
requires updating it to the actual HTTPS resource URL.

The verifier accepts RS256-signed Keycloak access tokens with the configured
issuer and audience, required subject/issued/expiry claims, `typ: Bearer`, valid
dates, and a maximum 15-minute lifetime. The development realm issues five-minute
tokens. ID tokens, expired tokens, wrong audiences/issuers, and other signing
algorithms are rejected. Signing keys come only from the configured JWKS URL;
token-supplied key URLs are not used. Redirects and proxy environment settings
are disabled for this fetch. Key sets are bounded, cached for five minutes,
and refreshed at most once per five seconds. An expired cache plus an unavailable
identity provider fails closed. Cached valid keys can remain usable until expiry.

`jwks_ca_file` optionally selects an absolute path to an operator-provided PEM
CA bundle. The default uses the system CA store. Both modes verify the TLS chain
and hostname; there is no setting to skip verification. The container TLS stack
mounts its public CA bundle at `/etc/chipload/identity-ca.pem`. Restart the API
after changing trust material.

The token's `resource_access.chipload.roles` sets a permission ceiling:
`shop:read` permits read actions, `shop:operate` permits `operation:read`,
`operation:execute`, `operation:cancel` and the three `code:*` actions; `shop:admin` permits all current
actions. Each action must **also** appear in the operator-owned binding for that
subject and shop. Grants may restrict operation actions through an `operations`
ID allowlist. `shop:operate` does not grant authoring privileges. No token
role alone grants access to a shop, and token-supplied shop grants are ignored.

After verifying an identity with `GET /v1/identity`, an operator can add a binding
to `.local/server.json`, for example:

```json
{
  "subject": "KEYCLOAK-SUBJECT-ID",
  "grants": [
    {
      "shop_id": "thermal-example",
      "actions": ["code:run", "code:read", "code:cancel", "source:read", "source:write", "build:read", "build:run"]
    }
  ]
}
```

Add `publication:write` only when publication is delegated. Restart the server
after editing its configuration; bindings are loaded at startup. The broker rereads explicit grants for each hosted SDK call. Runtime grant
administration and immediate token revocation are not implemented. Keep access
tokens out of source bundles, query strings, and logs. Send them in an
`Authorization: Bearer ...` header. Invalid supplied credentials never fall back
to anonymous access.

Production settings require HTTPS issuer, JWKS, and resource URLs. Development
HTTP configuration is limited to loopback. This validation is not a replacement
for the remaining production deployment work.

## MCP connection

Connect a Streamable HTTP client to `http://localhost:8088/mcp`. The server uses
the official MCP Python SDK and supports its current 2026-07-28 protocol.
The four tools are `run_python`, `read_run`, `cancel_run` and `discover_shop`.
Use [Python programs](PYTHON_API.md) to compose authenticated service calls.
Tools use a single typed `request` argument. For example:

```python
import asyncio
from mcp import Client

async def main():
    async with Client("http://localhost:8088/mcp") as client:
        result = await client.call_tool(
            "discover_shop", {"request": {"shop_id": "thermal-example"}}
        )
        print(result.structured_content)

asyncio.run(main())
```

Resources include `chipload://sdk/reference`, `chipload://sdk/python`,
`chipload://identity/current` (authenticated), and
`chipload://shops/{shop_id}/catalog`. Source projects are never resource URIs
that map to host files. Public tools/resources are anonymous; protected calls
without credentials receive HTTP 401 and a protected-resource metadata challenge.
Metadata is served at `/.well-known/oauth-protected-resource/mcp`, advertising
the configured Keycloak issuer. Pre-register the client with Keycloak and supply
its supported OAuth configuration; automatic client registration is not enabled
by Chipload. A bearer token can also be supplied through the SDK HTTP transport.

Use the SDK to handle protocol metadata. Raw 2026-07-28 HTTP calls must include
matching `MCP-Protocol-Version`, `Mcp-Method`, and (when applicable) `Mcp-Name`
headers alongside the body metadata. The SDK rejects mismatches. See the
[MCP HTTP specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
and [authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization).

## Errors and limits

REST domain errors use `{"error":{"code":"...","message":"..."}}`; validation
errors include field paths without echoing request values. MCP application
errors use an error tool result with the same error object and an HTTP-equivalent
`status` in structured content. Protocol-level validation remains the SDK's
responsibility.

Requests are bounded to 3 MiB, body reception to ten seconds, and simultaneous
active requests to 32 per application process. Only JSON with unique keys and
identity encoding is accepted. Host and Origin are checked against configuration.
Cross-origin browser access requires explicit `allowed_origins`; response policy
does not reflect arbitrary origins. Forwarded headers are not trusted by the
local launcher.

JSON must be UTF-8, with finite numbers of at most 100 characters, no duplicate
keys, at most 32 nested containers, and at most 100,000 scan tokens (structural
punctuation, string openings, and escapes). Rejected JSON returns HTTP 400
`invalid_json` on both REST and MCP before domain work. Source strings containing
ordinary JSON punctuation do not consume nesting depth. Model collection,
evaluation, and acceptance-suite limits are exposed by `chipload://sdk/reference`; see
[model admission limits](SHOP_MODEL.md#units-identities-and-artifacts) and
[build contracts](AUTHORING.md#project-and-build-contracts).

API domain operations run in two [supervised service processes](SERVICE_WORKERS.md)
with address-space, CPU and wall-time limits. Saturation returns `503 data_busy`;
interruption returns `503 service_interrupted` without replaying the request.
An interrupted mutation may already have committed. The documented loopback
HTTP measurements cover service capacity; internet load qualification remains open.

Source/build and [Python run](PYTHON_API.md) admission use durable quotas shared by every API and broker using
the same database. Limits apply to the deployment, shop, and verified identity;
switching transports or shops does not reset identity usage. Rejections use
HTTP 429 `quota_exceeded` with `quota` and `retry_after` in the error object. REST
also supplies `Retry-After` when retrying later can help; MCP uses the same error
and structured status 429. Storage/retained-record limits have `retry_after: null`
and require operator action. No rejected build is created or charged.

Reusing an accepted build's request key still returns that build when quotas are
full. Reusing the key for a different source remains a 409 conflict. Keep the
same key when retrying an uncertain submission; a new key requests a new build.
See [quota defaults and operator controls](AUTHORING.md#authoring-and-build-quotas).
These controls cover source/build storage and work admission. Filesystem quotas
and retention/backup operations remain deployment work.

Validated terminal build records and serialized public catalogs share a bounded
LRU cache within each service process. `data_cache_bytes` defaults to 33,554,432
(32 MiB) across the API's two workers; set it to zero to disable retention. Each
worker receives half that estimated Python-object memory allowance and a
128-entry limit. Oversized entries are returned without retention. Cache memory
accounting is separate from the enforced process address-space limit.

The cache implementation shares concurrent cold loads within a process, with a
four-load limit and a five-second wait. API service workers each dispatch one
call at a time; their independent caches do not share loads with one another.
Calls arriving while both workers are occupied return REST HTTP 503 `data_busy`,
or an MCP tool error with structured status 503. A read can be retried; a worker
interrupted during a write requires inspection of its durable record first.

The database selects the current publication on every request. Build cache keys
include all returned record metadata and a hash of the exact stored report
bytes, so corrupted or changed records are revalidated. Cached catalogs contain
only the explicit public projection and callers receive independent containers.
Permissions, observation snapshots, timestamps, and evaluation results are not
cached. Withdrawal blocks subsequent reads even if an old artifact remains in
memory; a request that already selected an earlier database snapshot may finish.
There is no cache TTL delaying publication, rollback, visibility, or withdrawal.

## Request admission

All HTTP routes, including MCP discovery, health checks, and CORS preflight,
share request budgets. Connection-peer and deployment checks precede token
verification and body parsing. Verified identities have an additional budget
across shops, routes, transports, and refreshed tokens.

| Budget in `policy.requests` | Refill per minute | Burst |
| --- | ---: | ---: |
| `deployment` | 6,000 | 200 |
| `peer` | 1,200 | 40 |
| `identity` | 1,200 | 40 |
| `mutation_identity` | 60 | 10 |
| `mutation_shop` | 120 | 20 |

These are token buckets with continuous refill, not counters that reset on a
minute boundary. The burst is the maximum accumulated allowance. Source saves,
build requests, publication changes, observation writes, code submissions and
cancellations share mutation
budgets through the common operation layer. Permission checks precede mutation
admission. Conflicts, no-op calls, and idempotent retries consume request allowance;
build request keys still prevent duplicate jobs. The local operator CLI uses
trusted services directly and is outside HTTP rate accounting.

Operators edit `policy.requests` using the existing
[`quotas` and `set-quotas` commands](AUTHORING.md#authoring-and-build-quotas).
The paired `PATH.admission` database stores policy and balances. Accepted checks and debits use a
[SQLite write transaction](https://www.sqlite.org/lang_transaction.html), so
API processes and restarts share the allowance. A failed combined check charges
none of its buckets. Later authentication, validation, or domain failures do not
refund earlier admission. Policy edits retain spent credit, capped by the new
burst, and update existing refill rates and expiration times atomically.
An initial read transaction can reject an exhausted caller without acquiring
the writer lock. A positive read never grants admission: policy, time and all
selected balances are recomputed under the write lock before charging. Validation
of identical immutable policy bytes is cached in at most eight entries; policy
selection, generations, balances and admission decisions remain uncached.
Shop source/build/publication writes use the main database and do not acquire
this accounting lock. Both files must be preserved together; see
[storage and recovery](AUTHORING.md#storage-authorization-and-recovery).

By default the peer is the ASGI connection address and forwarding headers are
ignored. `trusted_proxy_ips` can name up to sixteen exact gateway IP addresses;
CIDRs, wildcards, multicast, and unspecified addresses are rejected. Only a
connection from one of those peers may supply `X-Forwarded-For`, and it must
contain exactly one literal IP. Missing, duplicate, malformed, or chained values
are rejected before token verification and body parsing. Other forwarding headers
do not select identity or budgets. Keep general proxy rewriting disabled in the
ASGI launcher.

The [TLS gateway](TLS.md#trust-and-network-boundaries) overwrites that header with
its connection peer and keeps the API on a private network. Do not trust a whole
network or accept a client-appended forwarding chain. IPv4-mapped IPv6 normalizes
to IPv4; IPv6 addresses share a `/64` allowance. Missing/invalid connection peers
share a fallback bucket. Clients behind NAT or an unconfigured upstream proxy
share their peer allowance; verified-identity limits still distinguish users.

`max_buckets` defaults to 4,096 and is bounded to 8–65,536. New peer/identity
records cannot evict active budgets. Fully replenished buckets are reclaimed
when new records need space. Backward clock changes grant no refill credit;
operators must maintain the host clock. Limits govern admitted application
work, while network flood and connection controls belong at the deployment edge.

Exhausted budgets return HTTP 429 with `rate_limited`, the budget name in `limit`,
and `retry_after` seconds. Limiter storage exhaustion, lock contention, or database
failure returns HTTP 503 `request_capacity`; database lock waits are limited to
50 ms. Both responses include `Retry-After` and `Cache-Control: no-store`.
The rate response follows [HTTP 429 semantics](https://www.rfc-editor.org/rfc/rfc6585.html#section-4).

MCP admission failures arrive before the request body is read: HTTP 429/503 carries
a JSON-RPC error with `id: null`, application error code 429/503, and the same
REST error plus `status` in `error.data`. Positive codes follow MCP's
[application error allocation](https://modelcontextprotocol.io/specification/2026-07-28/basic/index#error-codes).
The official Python client preserves those fields in `MCPError.data`; the client
test verifies retry after refill using the same connection. Mutation failures
after admission use the existing MCP error tool result, with structured status
429/503 and retry details. Clients should respect the delay and retain the same
build request key when retrying a submission.

Measure the limiter separately from HTTP, authentication, and shop evaluation:

```sh
uv run python scripts/benchmark-requests --calls 2000 --processes 4
uv run python scripts/benchmark-requests --calls 2000 --processes 4 --policy defaults
uv run python scripts/benchmark-requests --calls 2000 --processes 4 --shop-writer
```

Measurements on the development host (Python 3.14.6), with four spawned processes
and 2,000 calls per run:

| Policy / shop write lock | Accepted | Rate-limited | Capacity errors | Elapsed including startup | Median / p95 admission |
| --- | ---: | ---: | ---: | ---: | ---: |
| Raised allowances / free | 1,903 | 0 | 97 | 3.98 s | 2.04 / 51.10 ms |
| Defaults / free | 421 | 1,546 | 33 | 2.78 s | 1.08 / 20.26 ms |
| Raised allowances / held | 1,912 | 0 | 88 | 4.24 s | 2.23 / 51.05 ms |

The capacity policy raises deployment/peer refill to 60,000 per minute and burst
to 10,000. Each worker has a 512 MiB address-space limit and 20-second CPU limit.
Counts include the initial burst. These component measurements expose SQLite
write contention; they are not HTTP throughput or a production capacity target.
The `--shop-writer` variant holds the shop database write lock throughout the
run, including worker startup. Request accounting uses its separate store, so
that lock does not prevent admission. Contention between request writers can
still produce temporary 503 responses; deployment performance qualification
remains open.

## Verification

```sh
uv run --extra server python -m unittest discover -s tests -v
CHIPLOAD_LIVE_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_dev_stack_live.py' -v
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_agent_lifecycle_live.py' -v
```

Tests exercise signed-token failures, shop/role intersections, REST/MCP result
parity, public filtering, pagination, revision conflicts, observation freshness,
the official MCP client over real HTTP, and real Keycloak-issued tokens.
Request-limit tests also launch two independent HTTP server processes, share
one allowance, restart a process, and read published data while a shop write is
open. Migration tests cover interrupted copies, competing processes, preserved
allowances, and missing or mismatched capacity stores.
The opt-in agent lifecycle test uses the official client and a separate broker
process to save broken source, inspect a failed container build, repair it,
publish, update, roll back, and withdraw; an ordinary HTTP client checks discovery.
