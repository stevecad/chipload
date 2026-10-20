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
The installed environment includes Chipload and its locked dependencies.

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

Per deployment, shop and identity, defaults allow 20 pending runs, one running
run, 120 admissions per hour, 1,000 retained records and 512 MiB of reserved run
storage. These are separate from build capacity, so a program can wait for its
build. Each retained run reserves 1,114,112 bytes. Operator quota fields are
`pending_programs`, `running_programs`, `programs_per_hour`, `retained_programs`
and `program_bytes`. Storage limits require operator action; no automatic record
purge recycles request keys. Expired queued runs terminate; expired running jobs
release capacity only after their container is removed. Recovery never reruns
interrupted code.

## Deployment and upgrade

Run the broker with `--settings /path/to/server.json` to enable Python runs. It
validates the private settings file and repository pairing, then reads current
bindings for each SDK call. The packaged runtime mounts the same settings file
read-only into API and broker. Recreate both containers after replacing a bind
mounted settings file; the API loads its own bindings at startup. The broker
runs one code loop alongside its build loop; `--once` processes at most one job.

Existing bindings need explicit `code:run`, `code:read` and, if delegated,
`code:cancel` actions. `shop:admin` and `shop:operate` roles permit these actions,
subject to those bindings; `shop:operate` still cannot author or publish source.
A role alone gives no shop access. Rebuild worker and runtime images together.

Stop old writers and back up before upgrade. Repository schema 7 adds durable
Python runs atomically after the schema-6 operation migration, preserving the
paired admission store. New backups include code history and require no queued
or running code jobs. Stop/cancel or let runs settle before taking a backup.
The backup format accepts the current database schema; keep the matching older
runtime for restoring pre-upgrade archives before migrating them.

These controls are a development baseline. Public edge exposure, stronger
isolation and deployment load/security qualification remain unfinished.
