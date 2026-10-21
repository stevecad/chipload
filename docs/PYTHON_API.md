# Shop automation in Python

An agent can run ordinary Python against Chipload's SDK. Functions, imports,
loops and helper modules compose a workflow; MCP supplies four tools for running
that code and discovering public capabilities. Shop definitions remain Python
projects with versioned source, build reports, acceptance cases and publication.

## MCP workflow

Read `chipload://sdk/python` for the client methods and execution contract,
`chipload://sdk/reference` for authoring/query schemas, and
`chipload://identity/current` for the authenticated caller's shop grants.

| Tool | Request | Purpose |
| --- | --- | --- |
| `run_python` | `shop_id`, `request_key`, `code`, optional `inputs`, `files`, `seconds` | Admit one bounded Python program |
| `read_run` | `shop_id`, `run_id` | Read its state, JSON result, logs and SDK call journal |
| `cancel_run` | `shop_id`, `run_id` | Request termination; completed calls remain committed |
| `discover_shop` | `shop_id`, `action`, `arguments` | Anonymous public `catalog`, `page`, `inspect`, `evaluate` or `variables` query |

Each tool takes a single `request` argument. The previous per-operation MCP
tools have been removed. REST routes remain independently available.

`run_python` returns a queued run record immediately. Poll `read_run` until its
state is `succeeded`, `failed`, `cancelled` or `interrupted`. The globals `client`
and `inputs` are supplied to the program. Assign JSON-compatible output to
`result`; `print` writes private logs. For example, submit this as `code` with
`inputs={"shop_id":"thermal-example"}`:

```python
shop = client.shop(inputs["shop_id"])
source = shop.source()
project = source["project"]
for file in project["files"]:
    if file["path"].endswith(".py"):
        file["content"] += "\n# Maintained through the Python SDK\n"
saved = shop.save(project, expected_head=source["revision"])
build = shop.build(saved["revision"], request_key="maintenance-build-001")
result = {"revision": saved["revision"], "build_id": build["id"]}
```

A subsequent program inspects that exact build and publishes it only when its
report passes. Builds may outlast a code run; keep their IDs and inspect them in
another run. Python can also poll briefly while the separate build worker runs.

```python
shop = client.shop(inputs["shop_id"])
build = shop.build_result(inputs["build_id"])
if build["state"] == "succeeded":
    publication = shop.publication()
    result = shop.publish(build["id"], expected_generation=publication["generation"])
else:
    result = build
```

Create a new shop using `chipload.project.SourceProject` and `SourceFile`, then
`shop.save(project, expected_head=None)`. Construct the source files and their
business/technical acceptance cases in Python; [the authoring guide](AUTHORING.md)
and [process examples](PROCESS_EXAMPLES.md) document those contracts. Helper
modules for the automation itself go in `files` as `{path, content}` records.
The installed environment includes Chipload and its locked dependencies, plus
any [operator-provisioned shop libraries](DEPENDENCIES.md). Source projects can
pin that environment with `dependencies_sha256`. The database-backed SDK service
uses the core interpreter and never loads shop libraries or their startup hooks.

The same evaluation method accepts `target_kind="offer"` for
[alternative routes](SHOP_MODEL.md#alternative-routes). A Python program can
inspect `shop.page("offers")` and call `shop.evaluate(query)` using the published
offer ID and its inputs. No additional MCP tool is needed.

An agent with `evidence:revoke` can call `shop.revoke_evidence(id,
expected_model_sha256=shop.evidence()["revision"], reason="...")`. Use
`shop.evidence_revocation(id)` to inspect the durable private receipt. Revocation
applies across source rollback; see [evidence revocation](API.md#evidence-revocation).

Shop source can declare `PythonCheck` functions for nonlinear technical and
business conditions. They run in the isolated build worker for acceptance
cases, and `shop.build_result(id)` includes their bound answers. Use
`shop.assess(query, expected_revision=..., request_key=...)` for a queued query,
then `shop.assessment(id)` in a later run. See [assessments](ASSESSMENTS.md) for
grants, cancellation and receipt freshness. `shop.evaluate(query)` stays data-only
and reports unknown when a required function answer is absent. See
[Python constraints](SHOP_MODEL.md#python-constraints) for the contract and limits.

The [ERP integration example](ERP_INTEGRATION.md) composes `shop.execute`,
`shop.execution`, `shop.observations` and `shop.set_observations` to read an
external fact and update readiness while retaining its source time. It uses the
same four MCP tools and can also run through the ordinary Python client.

Public discovery needs neither a login nor code execution:

```json
{"request":{"shop_id":"thermal-example","action":"variables","arguments":{"query":{"ids":["customer-qualification"]}}}}
```

## Ordinary Python clients and REST

The same SDK works from an application or local script:

```python
from chipload.client import Client

client = Client.connect("https://shop.example", token=get_access_token, ca_file="company-ca.pem")
shop = client.shop("thermal-example")
result = {"catalog": shop.catalog(), "policies": shop.variables({})}
```

`get_access_token` is an application-supplied callable returning a current
Keycloak bearer token. Omit `token` for public discovery; omit `ca_file` to use
system trust. The client verifies certificate chains and names, ignores proxy
environment variables and makes no automatic retries or redirects. Development
HTTP requires explicit `allow_http_loopback=True` and a loopback origin.

`ServiceError` exposes `status`, `detail`, `code` and `retry_after`.
`TransportError` means no usable response arrived; a mutation may have committed.
Returned values are JSON dictionaries. Source projects and queries may also be
passed as SDK models. `client.call(operation, request)` provides the common
service contract when composing generic tooling.

Hosted Python is also available through REST:

| Method and path | Python client | Grant |
| --- | --- | --- |
| `POST /v1/shops/{shop}/runs` | `shop.run(code, request_key=..., inputs=...)` | `code:run` |
| `GET /v1/shops/{shop}/runs/{id}` | `shop.run_result(id)` | `code:read` |
| `POST /v1/shops/{shop}/runs/{id}/cancel` | `shop.cancel_run(id)` | `code:cancel` |

A REST submission body uses the same fields as `run_python`, including the shop
ID. The server returns HTTP 202. A code worker cannot submit or inspect other
code jobs through its SDK transport.

## Authority, failures and bounds

The broker runs code in a disposable container with no network, host mounts,
service tokens, database, Docker socket or equipment credentials. A pipe carries
SDK requests to the trusted broker. Every call passes through the existing
service validation, permission checks, identity request budget and mutation
budget. A program cannot call operator-only commands or provision adapters.

At admission, the verified caller's role-limited grants are captured for the
selected shop. Before execution and every SDK call, the broker intersects that
ceiling with the current operator-owned settings. Later grant additions cannot
expand an admitted program. Removing a grant prevents subsequent calls once the
broker sees the changed settings. JWT expiry does not cancel an already admitted
job; Keycloak role changes alone do not provide immediate job revocation.
Private calls to another shop are denied; explicitly public discovery remains
available. `code:read` exposes private run output to that shop's authorized
readers, so grant it with the sensitivity of the code's output in mind.

The same caller, shop, request key and exact program return the same record.
Changing any program field while reusing its key returns 409. A failed,
interrupted or cancelled program is never automatically rerun. A new key admits
a new program that can cause new effects.

Before each SDK call, its operation and request digest are journaled as
`started`. A returned call records status and safe record IDs. A crash can leave
a `started` entry even when its service mutation committed. An error response
can also follow a commit; the recorded status does not establish rollback. Inspect source,
build, publication or execution records before continuing. This is not a
transaction across calls: cancelling code cannot undo a publication, cancel a
build or prove that an accepted equipment action had no effect. Physical
operations retain their own durable execution and reconciliation records.

Runs allow up to 30 seconds, 64 SDK calls, 256 KiB of submitted program data,
256 KiB of JSON result, 64 KiB of raw logs and 32 MiB of total RPC traffic.
Individual RPC requests are limited to 3 MiB and responses to 8 MiB. Source code
is limited to 64 Ki characters, with up to 32 helper files. Existing JSON depth
and complexity limits also apply. CPU, memory, process and filesystem limits
match the [source worker](AUTHORING.md#container-boundary). Container creation and
cleanup have additional bounded timeouts; this is not a realtime execution
service. Raw stdout protocol violations fail the run. Output remains untrusted
user-authored data, never an authoritative acceptance report.

Each hosted run has a separate trusted SDK service container. Python's SDK calls
are mediated by the broker with the current caller scope, then executed in that
container. It mounts only the dedicated repository directory and has no network,
Docker socket, settings file or secret mount. It runs as the non-root repository
owner with a read-only image, 256 MiB of memory/address space, one CPU, 64 file
descriptors and the source worker's process/capability restrictions. It retains
up to 32 MiB of immutable cache for that run. Database access still makes this
service trusted implementation code; shop Python never executes there.

An SDK call has a two-second CPU budget (plus less than one second of rounding)
and a five-second wall deadline, shortened by the Python program's remaining
wall budget. Cancellation, shutdown and claim fencing are checked while waiting
for the service response. A service watchdog also stops blocked calls and exits
an idle orphan after 60 seconds. A failed or missing SDK response interrupts the
program and leaves its call journal entry `started`; the engine never resends
the call. Recovery confirms removal of both the code and SDK service containers
before releasing running capacity. A cleanup failure keeps the run unsettled.
The code and SDK containers share a 20-second monotonic cleanup budget, with a
share reserved for each. Concurrent removal or a lost Docker reply is resolved
by inspecting for explicit absence. An inspection error or a container still
present at the deadline keeps capacity reserved for a later recovery attempt.
This replaces the earlier two-second competing-removal check that expired during
concurrent container tests. Recovery's 30-second claim covers one cleanup attempt
and its completion transaction. Cleanup never resubmits Python or an SDK call.
Both containers also have durable creation intent. Recovery cannot release the
run while either creation is unresolved, even if that container is currently
absent. A late acknowledgement or observed container permits cleanup; it never
restores the expired claim or replays the program. See
[authoring recovery](AUTHORING.md#storage-authorization-and-recovery) for the local
`recover-launch python RUN_ID` command. It can reserve unresolved names from the
current cleanup protocol with inert containers before settling a run. Reservations
persist and are reinstalled before execution after restore. Legacy removal by
name requires separate maintenance.

Per deployment, shop and identity, defaults allow 20 pending runs, one running
run, 120 admissions per hour, 1,000 retained records and 512 MiB of reserved run
storage. These are separate from build capacity, so a program can wait for its
build. Each retained run reserves 1,114,112 bytes. Operator quota fields are
`pending_programs`, `running_programs`, `programs_per_hour`, `retained_programs`
and `program_bytes`. Storage limits require operator action; no automatic record
purge recycles request keys. Expired queued runs terminate; expired running jobs
release capacity only after both containers are removed. Recovery never reruns
interrupted code.

## Deployment and upgrade

Run the broker with `--settings /path/to/server.json` to enable Python runs. It
validates the private settings file and repository pairing, then reads current
bindings for each SDK call. The packaged runtime mounts the same settings file
read-only into API and broker. Recreate both containers after replacing a bind
mounted settings file; the API loads its own bindings at startup. The broker
runs one code loop alongside its build loop; `--once` processes at most one job.

The database parent directory must be private, owned by the service user, and
contain only the shop database, its `.admission` partner and their SQLite journal
files. The host API example uses `.local/data/chipload.db`; run the broker with
the matching `--database` argument. For a containerized broker,
`--service-data` supplies the directory's absolute path on the Docker host; the
shipped Compose files supply it automatically. The service checks the repository
pair identity before accepting calls. Remote Docker daemons are rejected for this
path because the database mount and SQLite storage must be local.

For an existing repository in a directory shared with settings or secrets, stop
all writers and use the [paired-store recovery procedure](AUTHORING.md#storage-authorization-and-recovery)
to place its databases in a dedicated directory. Update the API settings and
broker/operator database paths together. No command silently relocates a database
or initializes a substitute when the service mount is missing or mismatched.

Existing bindings need explicit `code:run`, `code:read` and, if delegated,
`code:cancel` actions. `shop:admin` and `shop:operate` roles permit these actions,
subject to those bindings; `shop:operate` still cannot author or publish source.
A role alone gives no shop access. Rebuild worker and runtime images together.

Stop old writers and back up before upgrade. Repository schema 9 added durable
name reservations and cleanup protocol markers after the schema-8 launch migration, preserving the
paired admission store. Settle running jobs first: migration preserves their
unknown creation outcomes as unresolved intent. New backups include code history and require no queued
or running code jobs. Stop/cancel or let runs settle before taking a backup.
The backup format accepts the current database schema; keep the matching older
runtime for restoring pre-upgrade archives before migrating them.
Schema 10 adds durable evidence revocations atomically. Preserve them with the
paired databases and explicitly delegate `evidence:revoke` to the intended agent.

These controls are a development baseline. Public edge exposure, stronger
isolation and deployment load/security qualification remain unfinished.

## Measure batched calls

```sh
./scripts/build-worker
uv run python scripts/benchmark-python --calls 1 --runs 3
uv run python scripts/benchmark-python --calls 10 --runs 3
uv run python scripts/benchmark-python --calls 10 --runs 3 --large-catalog
```

This benchmark evaluates the synthetic 17-step workflow used by
`benchmark-data`, through actual code and SDK service containers. Each program
has a fresh service cache. Every result and completed call journal is checked;
any refusal or interruption fails the benchmark. Admission uses default policy.
Temporary storage and only the benchmark's own containers are removed.

Measurements on 2026-10-20, on a shared Linux x86-64 development host with 16
logical CPUs, used Python 3.14.6 for the broker/client and 3.12.15 in the restricted
containers. Three runs per case produced:

| Workload | Calls per program | Median complete run | Completed SDK calls/s |
| --- | ---: | ---: | ---: |
| Compact | 1 | 6.88 s | 0.137 |
| Compact | 10 | 7.38 s | 1.344 |
| Large catalog | 10 | 9.23 s | 1.115 |

These times include creation and cleanup of both containers, service startup,
SQLite, grants and durable call journals. They exclude HTTP/TLS/JWT and source
builds. Container lifecycle cost dominates short programs, so compose related
SDK calls in one program when their workflow permits it. This is a small
component measurement under concurrent development activity, not sustained
deployment capacity or a latency guarantee. Reducing container lifecycle cost
without weakening cancellation, claim fencing or cleanup is further work.
