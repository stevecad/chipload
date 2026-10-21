# Connecting a shop to an ERP

Shop Python can read an external business fact through an authorized operation
and use a derived observation when evaluating readiness. The ERP owns inventory,
orders, purchasing, production planning, accounting and invoicing. Chipload owns
the shop definition, its capability/business rules and operation contracts.

[The example](../examples/erp_shop.py) describes a prototype service requiring
one unit of material. Installed capability is independent of stock; readiness
also requires a fresh stock observation. This policy belongs to the example's
Python code. The engine has no ERP vendor or inventory-specific behavior.

## External references

An `Observation` can carry an optional `external` object:

```json
{
  "system": "example-erp",
  "record": "material-stock-demo",
  "revision": "stock-v1"
}
```

`system` is a Chipload ID chosen for the source system. `record` is a nonblank
opaque string of at most 512 characters; `revision` is an optional nonblank
string of at most 200 characters. An unknown revision stays null. These values
are metadata, not URLs to fetch, credentials or proof that a source is trusted.
The generic model supports sources other than ERPs.

References are retained in authorized observation snapshots. Public catalog and
evaluation projections omit them. Source, original observation time and maximum
age retain their normal meanings. Copy only the data needed for the shop rule;
private observations still require appropriate access controls.

## Read contract and authority

The example declares a private `read-material-stock` operation with `effect="read"`.
Its business prerequisite permits only `material-stock-demo` for the purpose
`shop-readiness`. Its typed result contains:

| Field | Meaning |
| --- | --- |
| `record-reference` | Exact requested external record |
| `record-revision` | Stable version of this source observation |
| `observed-at` | Source timestamp with a time zone |
| `available-units` | Optional nonnegative whole-unit quantity, with unit `1` |

Missing quantity means unknown. The mapper rejects other records, invalid
timestamps, fractional/negative quantities and incompatible units. A failed or
indeterminate read cannot create a fresh observation.

The deployment operator separately binds the exact published contract to a
trusted adapter. For a network integration, use the
[HTTPS receiver contract](ADAPTERS.md) and a scoped OpenBao credential. The
receiver maps its vendor's API to this operation and enforces its own read-only
authority. Declaring `effect="read"` does not restrict a receiver's credentials.
Neither shop source nor hosted Python receives ERP credentials or direct network
access. The example and tests use explicit simulation; they qualify no vendor
connector.

The synchronization identity needs `operation:execute` and `operation:read`
restricted to this operation, plus `observations:read` and `observations:write`.
Hosted execution also needs `code:run`, `code:read` and optionally `code:cancel`.
Private evaluation needs `evaluation:read`. Keycloak role ceilings and the
operator's shop grants both apply. Source editing, publication and adapter
binding remain separate authorities.

Observation grants currently cover the whole shop snapshot. The helper's merge
rules prevent accidental replacement of other feeds; they are not server-side
permissions for individual feeds. Grant snapshot writes only to trusted shop
automation.

## Compose the workflow in Python

Include the example module as `bridge.py` in the program's `files`. With the
example published and its simulated adapter bound, submit:

```python
from bridge import SHOP, read_stock

result = read_stock(client.shop(SHOP), request_key="stock-read-001", mode="simulated")
```

Keep the returned execution ID. Inspect it through `shop.execution` in a later
run; the separate executor performs the read. Retrying the same complete request,
including its key and publication generation, returns that execution. Changed
inputs or generation cannot reuse the key. A new read needs a new key. Once it succeeds:

```python
from bridge import SHOP, sync_stock

result = sync_stock(client.shop(SHOP), inputs["execution_id"])
```

`sync_stock` checks the receipt's publication and adapter binding, derives the
one-unit boolean, and merges it into the current snapshot using
`expected_revision`. It preserves unrelated observations and retries up to three
times only after a definite revision conflict. Its observation keeps the
provider timestamp and version; replaying a receipt cannot refresh its age.
Simulated results stay simulated. Live derived results use source `calculated`.

This example requires a stable source version. Exact repeats are unchanged;
older versions by timestamp are ignored; changed data under one version and
conflicting versions at the same time are rejected. Opaque versions are not
sorted. A provider with different version semantics needs its own mapping.
An existing observation ID belonging to another source is not adopted.

A lost write response propagates as a transport error because the write may
have committed. An explicit later retry first reads the snapshot and can return
`unchanged` without another write. The publication/binding check and observation
write are separate calls; the write guard covers the observation revision.
Keep ID meanings permanent and use new IDs when their meaning changes.

None of these reads reserves material or schedules work. A fresh supported
answer can become stale; a failed refresh leaves the earlier timestamp intact.

## REST clients

An ERP or ordinary application can use the same [REST API](API.md) without MCP:

- Public catalog and evaluation: `GET /v1/shops/{shop}/catalog` and
  `POST /v1/shops/{shop}/evaluate`.
- Authorized operation submission and receipts:
  `POST /v1/shops/{shop}/operations/{operation}/executions` and
  `GET /v1/shops/{shop}/operations/{operation}/executions/{execution}`.
- Authorized observation snapshots: `GET` and `PUT` at
  `/v1/shops/{shop}/observations`, with the current `expected_revision` for writes.

The [ordinary Python client](PYTHON_API.md#ordinary-python-clients-and-rest)
provides those calls too. An external client may run the mapping locally with
its own scoped token. Publication authorizes public discovery; private records
and execution results still require their corresponding grants.

## Run the example and checks

```sh
uv run python -m examples.erp_shop
uv run --extra server python -m unittest discover -s tests -p 'test_erp_interop.py' -v
./scripts/build-worker
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_erp_interop_live.py' -v
```

The example has ten technical, business and freshness cases. Focused tests cover
retry identity, lost responses, concurrent merges, invalid output, stale data,
permissions and public filtering. The live check builds the actual source and
runs the mapper through MCP Python and its contained SDK service, then queries
REST. Its trusted simulated adapter reads a separate fixture record and verifies
that the record is unchanged. A real vendor connector, vendor permissions and
failure recovery still need integration-specific qualification.
