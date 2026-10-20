# REST and MCP

Chipload serves REST and Streamable HTTP MCP from one application. Both call the
same operation contracts, application services, permission checks, and published
revisions. Public discovery works without authentication or an agent.

The current server is a local development runtime. It binds to loopback and does
not import shop source or invoke Docker. A separate broker processes build jobs.
Production packaging, TLS deployment, ingress rate limits, stronger isolation,
recovery exercises, and load/security qualification remain on the roadmap.

## Start the services

```sh
uv sync --locked --extra server
./scripts/dev-stack up
./scripts/build-worker
cp deploy/api/settings.example.json .local/server.json
uv run --extra server python -m chipload.server --config .local/server.json
```

In another terminal:

```sh
uv run python -m chipload.broker
```

The server and broker default to `.local/chipload.db`. The broker requires Docker
access and `.local/worker-image`; the server requires neither. Each broker recovers
expired claims and processes one build at a time. Multiple brokers share atomic
dispatch and running-job limits in the database. Eligible shops rotate by their
last dispatch turn, with identity turns and queued time breaking ties. New
queues join at the current turn. Shutdown lets the current bounded job finish.
`--once` processes at most one eligible job, which is useful in tests.

Use the [local authoring walkthrough](AUTHORING.md) to publish an example before
trying anonymous discovery. Publication through that CLI and through the network
interfaces selects the same database record.

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

| REST endpoint | MCP tool |
| --- | --- |
| `GET /v1/sdk` | `sdk_reference` |
| `GET /v1/identity` (authenticated) | `identity_read` |
| `GET /v1/shops/{shop}/catalog` | `shop_catalog` |
| `GET /v1/shops/{shop}/{collection}` | `shop_list` |
| `GET /v1/shops/{shop}/{collection}/{id}` | `shop_inspect` |
| `POST /v1/shops/{shop}/evaluate` | `shop_evaluate` |
| `POST /v1/shops/{shop}/variables/query` | `shop_variables` |

Collections are `sites`, `resources`, `catalogs`, `processes`, `capabilities`,
`workflows`, and `variables`. Only explicitly public records appear. Private and
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

If a required input catalog contains hidden allowed values, public evaluation
returns unknown without testing guessed values against the hidden set. Public
workflows with private steps also remain unknown. Publication does not turn
partial disclosure into proof of support.

## Authenticated operations

| REST endpoint | MCP tool |
| --- | --- |
| `GET /v1/shops/{shop}/source?revision=...` | `source_read` |
| `PUT /v1/shops/{shop}/source` | `source_save` |
| `POST /v1/shops/{shop}/builds` | `build_request` |
| `GET /v1/shops/{shop}/builds/{build}` | `build_inspect` |
| `GET /v1/shops/{shop}/publication` | `publication_read` |
| `PUT /v1/shops/{shop}/publication` | `publication_change` |
| `GET /v1/shops/{shop}/audit?after=0&limit=100` | `audit_read` |
| `GET /v1/shops/{shop}/evidence` | `evidence_read` |
| `GET /v1/shops/{shop}/observations` | `observations_read` |
| `PUT /v1/shops/{shop}/observations` | `observations_write` |
| `POST /v1/shops/{shop}/private/evaluate` | `shop_evaluate_private` |
| `POST /v1/shops/{shop}/private/variables/query` | `shop_variables_private` |

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
Required permissions are `observations:write`, `observations:read`,
`evidence:read`, and `evaluation:read` for the corresponding private operations.
Authoring permissions are listed in [AUTHORING.md](AUTHORING.md).

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

The token's `resource_access.chipload.roles` sets a permission ceiling:
`shop:read` permits read actions, and `shop:admin` permits the current authoring
actions. Each action must **also** appear in the operator-owned binding for that
subject and shop. `shop:operate` does not grant authoring privileges. No token
role alone grants access to a shop, and token-supplied shop grants are ignored.

After verifying an identity with `GET /v1/identity`, an operator can add a binding
to `.local/server.json`, for example:

```json
{
  "subject": "KEYCLOAK-SUBJECT-ID",
  "grants": [
    {
      "shop_id": "thermal-example",
      "actions": ["source:read", "source:write", "build:read", "build:run"]
    }
  ]
}
```

Add `publication:write` only when publication is delegated. Restart the server
after editing its configuration; bindings are loaded at startup. Runtime grant
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
Tools use a single typed `request` argument containing the same contract as the
REST operation. For example:

```python
import asyncio
from mcp import Client

async def main():
    async with Client("http://localhost:8088/mcp") as client:
        result = await client.call_tool(
            "shop_catalog", {"request": {"shop_id": "thermal-example"}}
        )
        print(result.structured_content)

asyncio.run(main())
```

Resources include `chipload://sdk/reference` and
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
evaluation, and acceptance-suite limits are exposed by `sdk_reference`; see
[model admission limits](SHOP_MODEL.md#units-identities-and-artifacts) and
[build contracts](AUTHORING.md#project-and-build-contracts).

These initial bounds do not constitute internet load qualification. Data
processing still needs deployment process limits, request-rate controls,
and concurrent load verification.

Source/build admission uses durable quotas shared by every API and broker using
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
These controls cover source/build storage and work admission. Public-request
rate limits, other mutation rates, filesystem quotas, and retention/backup
operations remain deployment work.

Validated terminal build records and serialized public catalogs share a bounded
process-local LRU cache. `data_cache_bytes` defaults to 33,554,432 (32 MiB);
set it to zero to disable retention. It limits estimated retained Python-object
memory, with a separate 128-entry limit. It is not a process RSS limit.
Oversized entries are returned without retention. Four distinct cold loads may
run concurrently; requests for the same data share their load. Saturation or a
five-second wait returns REST HTTP 503 `data_busy`, or an MCP tool error with
structured status 503. Retrying a read is safe; a waiting request timing out
does not cancel or restart the original load.

The database selects the current publication on every request. Build cache keys
include all returned record metadata and a hash of the exact stored report
bytes, so corrupted or changed records are revalidated. Cached catalogs contain
only the explicit public projection and callers receive independent containers.
Permissions, observation snapshots, timestamps, and evaluation results are not
cached. Withdrawal blocks subsequent reads even if an old artifact remains in
memory; a request that already selected an earlier database snapshot may finish.
There is no cache TTL delaying publication, rollback, visibility, or withdrawal.

## Verification

```sh
uv run --extra server python -m unittest discover -s tests -v
CHIPLOAD_LIVE_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_dev_stack_live.py' -v
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_agent_lifecycle_live.py' -v
```

Tests exercise signed-token failures, shop/role intersections, REST/MCP result
parity, public filtering, pagination, revision conflicts, observation freshness,
the official MCP client over real HTTP, and real Keycloak-issued tokens.
The opt-in agent lifecycle test uses the official client and a separate broker
process to save broken source, inspect a failed container build, repair it,
publish, update, roll back, and withdraw; an ordinary HTTP client checks discovery.
