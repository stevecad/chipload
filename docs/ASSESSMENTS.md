# On-demand shop assessments

An assessment evaluates a capability, workflow or alternative offer, including its
published Python technical and business rules. REST clients and agents use the
same queued operation. The ordinary `/evaluate` endpoint remains a synchronous
data query; it reports unknown when a required Python answer is absent.

## Request and inspect

An authenticated caller needs an explicit `evaluation:run` grant for the shop.
Keycloak's `shop:operate` or `shop:admin` role permits that grant; the role alone
does not grant shop access. Assessment reads and cancellation belong to the
requesting identity. They require no source, build or equipment permission.
Anonymous catalog discovery and data queries remain available.

Submit `POST /v1/shops/{shop_id}/assessments` with:

```json
{
  "shop_id": "example",
  "request_key": "fit-check-001",
  "expected_revision": "<catalog public_sha256>",
  "query": {
    "target_kind": "capability",
    "target": "service",
    "facts": []
  },
  "seconds": 30,
  "private": false
}
```

Supply the facts required by the shop's published contract. The response is a
202 receipt with `id`, `state`, timestamps and `cancel_requested`. Poll
`GET /v1/shops/{shop_id}/assessments/{id}`. A successful job includes `evaluation`
using the ordinary public evaluation projection. Job success means the query
completed; its domain outcome can be supported, unsupported, conditional or
unknown. Worker errors, malformed answers, timeouts and output overflow produce
failed or interrupted jobs with no domain answer. Source, logs, exceptions,
internal hashes and private identifiers are withheld from public receipts.
To diagnose a failing shop function, its author can add the request as an
acceptance case and rebuild; the private build report identifies failing cases
and functions.

`POST /v1/shops/{shop_id}/assessments/{id}/cancel` requests cancellation and returns
status only. Queued jobs cancel immediately; running jobs retain their capacity
until worker cleanup is confirmed. Cancellation remains available if the caller
loses private evaluation access but retains `evaluation:run`.

Agents use the existing `run_python` MCP tool and these Python client methods:

```python
shop = client.shop(inputs["shop_id"])
result = shop.assess(
    inputs["query"],
    expected_revision=shop.catalog()["public_sha256"],
    request_key=inputs["request_key"],
)
```

In a later run, call `shop.assessment(id)` or `shop.cancel_assessment(id)`.
Assessments and agent Python programs share running capacity, so a program
should submit and return instead of waiting in a polling loop while holding
the shop's only slot. MCP still exposes four tools.

Exact retries reuse the same receipt without another admission charge. A changed
request with the same key conflicts. Request keys share the shop/identity
namespace with Python runs. Use a new key for a fresh assessment.

For detailed private results, set `private=true` and additionally grant
`evaluation:read`. Use the model hash from an authorized private evaluation as
`expected_revision`. Private reads continue to require that grant. Public jobs
never evaluate hidden conditions: an incomplete public contract returns unknown
without invoking its functions.

## Meaning and freshness

An assessment is an **as-of receipt**, with the evaluator's `evaluated_at` time.
It is not a continuously updated answer or an equipment authorization. Source
functions consume request facts; they do not receive observations, credentials,
service clients or workflow state. The trusted evaluator combines their bounded
answers with all static requirements, current observations, evidence and
revocations after the worker finishes. A function cannot override those checks.

The service binds admission to an exact publication generation, build and source
revision. It rechecks that publication after execution and before returning a
completed receipt. Changed publication, observations or evidence revocations
produce `state=superseded` and withhold the old answer. Withdrawing and then
republishing the same build still supersedes prior receipts. Elapsed time alone
does not rewrite a historical receipt: inspect `evaluated_at` and the freshness
summary, and submit a new request when a current answer is needed. Assessments
are never substituted into ordinary live queries or operation preconditions.

## Execution and operation

The broker executes the published source in the build's exact SDK, dependency
and image environment. The configured worker must match; changing environments
requires rebuilding and publishing the shop before its functions can run again.
The source factory initializes module state and must reproduce its published
model. The trusted broker retains the published static model and selects the
check declarations; source cannot replace them in a query response.

The disposable source container has no network, database mount, Docker socket,
host files or credentials. CPU, memory, processes, output and wall time remain
bounded. The complete source/query envelope also passes bounded JSON admission.
The broker requires exactly the requested check identities and valid outcomes,
then computes the result itself. Arbitrary Python can depend on process state or
the system clock; this execution path does not guarantee mathematical purity.

Assessments use existing `pending_programs`, `running_programs`,
`programs_per_hour`, `retained_programs` and `program_bytes` quotas at deployment,
shop and identity scopes. Request and mutation rate limits also apply. Durable
creation intent, immutable container IDs and confirmed cleanup protect running
capacity across cancellation and recovery. An interrupted worker is never
automatically replayed. Start the broker with its operator settings to enable
this queue, as for hosted Python.

Repository schema 11 adds a job-kind discriminator to the shared queue. Stop old
API, broker and executor writers before upgrading; migration from schema 10 is
atomic and preserves existing jobs and spent allowances. Rebuild worker/runtime
images. Backup requires all code and assessment jobs settled and preserves their
typed histories. The grants are explicit: existing bindings need
`evaluation:run` added by the operator before callers can submit assessments.

`test_assessments.py` covers authority, snapshots, limits and migration. Run the
live check with rebuilt images:

```sh
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_assessments_live.py' -v
```

It exercises actual source builds, all 30 example cases through REST, MCP-hosted
Python, cancellation, hostile functions and container isolation.
