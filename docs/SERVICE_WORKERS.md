# Supervised service workers

REST and MCP dispatch domain operations through two persistent service processes
inside the API container. Each process validates stored artifacts, evaluates
capabilities and business rules, and applies the existing service permissions.
The HTTP process handles ingress, JSON/request validation, identity verification,
IPC framing and response serialization. Agent-authored Python continues to run in
separate [restricted containers](PYTHON_API.md).

Service workers execute trusted Chipload implementation code. They share the
API container's filesystem and network authority, including access to the shop
and admission databases. This boundary contains resource exhaustion and allows
termination of stuck service work; it is not a sandbox for untrusted Python or
a defense against a compromised service implementation. The broker's mediated
Python SDK calls still execute in the broker process. Source workers and
operation executors retain their separate containment boundaries.

## Limits and lifecycle

| Control | Default |
| --- | --- |
| Concurrent service calls per API instance | 2, with no waiting queue |
| Address space per service process | 256 MiB |
| CPU budget per call | 2 seconds plus less than 1 second of rounding |
| Supervisor wall deadline | 5 seconds, including replacement startup |
| IPC request / response | 4 MiB / 8 MiB, bounded JSON |
| File descriptors / core dumps | 64 / disabled |
| Process recycling | Before the next call after 1,000 completed calls |
| API container | 1 CPU, 1 GiB memory with no additional swap, 128 processes |

Limits are applied before SDK imports. Workers start with an explicit minimal
environment, isolated Python import settings, closed inherited descriptors and
their own process group. A Linux process descriptor watches the API process;
if that process exits, the worker kills its own group. The supervisor also kills
the group on timeout, malformed output or shutdown, and confirms termination
before reusing the slot. If termination cannot be confirmed, the slot stays
unavailable. Calls already admitted to other slots can finish.

CPU limits track accumulated process CPU, extending the soft threshold before
each request. The supervisor's monotonic wall deadline also covers blocked I/O.
Two seconds is not a precision scheduler guarantee. See Python's
[resource limit semantics](https://docs.python.org/3/library/resource.html).

The packaged Linux image supplies the required `pidfd_open` libc entry point
and kernel support; process-descriptor access must also be permitted by the
deployment's syscall policy. A missing facility causes worker startup to fail.
Run the API in the supplied Linux image when the host Python environment lacks
these facilities. API startup waits for both workers to initialize before it
accepts requests. `/healthz` remains a liveness endpoint and does not perform a
domain query or certify remaining pool capacity.

The `data_cache_bytes` setting is divided across the two workers (16 MiB each
with the default 32 MiB setting). Caches are independent, so two concurrent cold
requests may each validate the same artifact. A record larger than a worker's
cache budget is revalidated on subsequent requests. The HTTP supervisor retains
no artifact cache. Publication pointers, permissions, observations and evaluation
results remain uncached. All writes use the same database transactions and shared
admission policy as the in-process service.

## Failures and retries

When both workers are busy, calls receive `503 data_busy` immediately, before
service dispatch. If a worker dies, exceeds its deadline or loses its response,
the call receives `503 service_interrupted`, with `retry_after: 1`. REST supplies
the corresponding `Retry-After` header; MCP returns the same structured error.
Permission denials, conflicts and quota refusals retain their domain status and
retry details. A failed call is never automatically replayed.

An interrupted mutation may have committed before the response was lost. Read
its durable source, build, publication, Python run or operation record before
continuing. Preserve request keys and expected revisions/generations on retries.
Cancellation or killing a service worker cannot undo a committed write or an
external action. A later request can replace a dead worker, but replacement does
not resume the earlier request.

## Reproduce measurements

```sh
./scripts/build-worker
./scripts/build-runtime
uv run --extra server python scripts/benchmark-service --container --calls 100 --concurrency 2
uv run --extra server python scripts/benchmark-service --container --calls 100 --concurrency 2 --large-catalog
uv run --extra server python scripts/benchmark-service --container --calls 100 --concurrency 4 --large-catalog
uv run --extra server python scripts/benchmark-service --container --calls 200 --concurrency 2 --policy defaults
```

The script seeds a disposable synthetic shop with a 17-step workflow and ten
range rules. It sends 75% workflow evaluations and 25% capability-page requests
over actual loopback HTTP. The compact artifact is 5,945 bytes; the large-catalog
artifact is 4,147,815 bytes. Successful responses are checked for the expected
domain result. Cold-call latency, every HTTP status, accepted throughput and
accepted-response median/p95 are reported separately. No request is retried.

`--policy capacity` raises only deployment/peer HTTP allowances in the temporary
database so the benchmark can measure service capacity. `--policy defaults`
keeps normal request policy and reports its rejections. `--container` verifies
the built API image against the checkout and applies the shipped CPU, memory,
process and filesystem restrictions. Without it, the API is a local subprocess
with a 512 MiB address-space limit, and no container CPU quota. Only resources
created by the benchmark are removed.

These measurements include HTTP, shared request accounting, SQLite, projection
and service processing. They exclude TLS, JWT verification, the gateway,
external network latency, simultaneous authoring, hosted Python execution and
equipment adapters. They do not establish internet deployment capacity.

On 2026-10-20, the four container commands above produced the following results
on a shared Linux x86-64 development host with 16 logical CPUs. The API image
used Python 3.12.15 with its one-CPU quota; the client used Python 3.14.6.

| Workload / policy | Clients | HTTP results | Accepted requests/s | Accepted p95 | First cold call |
| --- | ---: | --- | ---: | ---: | ---: |
| Compact / capacity | 2 | 100 × 200 | 36.82 | 87 ms | 66 ms |
| Large catalog / capacity | 2 | 100 × 200 | 12.42 | 189 ms | 1,080 ms |
| Large catalog / capacity | 4 | 13 × 200; 87 × 503 | 4.88 | 2,663 ms | 1,102 ms |
| Compact / defaults | 2 | 103 × 200; 97 × 429 | 31.90 | 85 ms | 60 ms |

The four-client burst returned 86 `data_busy` errors and one `request_capacity`
error from shared admission contention. Default-policy refusals were all
`rate_limited`. No run reported `service_interrupted`. Rejections are included in
elapsed throughput accounting; p95 describes only successful requests. A burst
that immediately consumes its request count through refusals does not measure
sustainable throughput with client backoff. Large cold reads remain slower than
warm reads, and either independent worker can encounter a cold cache.

Initial regression targets for these 100-request, two-client capacity fixtures
on a comparable host are: all requests succeed, no worker interruption, compact
throughput at least 25 requests/s with p95 at most 150 ms, and large-catalog
throughput at least 8 requests/s with p95 at most 300 ms. The first large cold
call should finish within two seconds. These targets guide development; they
are not a production SLA. Qualifying public traffic also requires the omitted
TLS/identity/network paths, representative tenant workloads, sustained load and
recovery under contention.

## Verification

```sh
uv run --extra server python -m unittest discover -s tests -p 'test_service_pool.py' -v
CHIPLOAD_RUNTIME_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_runtime_live.py' -v
```

Tests use actual subprocesses to exercise CPU loops, memory exhaustion, blocked
calls, malformed replies, saturation, failed cleanup, parent death, recycling,
authorization and publication changes. A mutation that commits and then exits
without replying is recorded once; an explicit retry with the same key returns
the same durable record. REST and MCP tests use the default process pool. The
container test checks actual service-process limits as well as a complete
source/build/publication lifecycle and container restart.
