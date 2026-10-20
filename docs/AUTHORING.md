# Authoring, builds, and publication

The local lifecycle is executable. Agents can save ordinary Python projects,
inspect the SDK schema, build in restricted containers, inspect diagnostics and
acceptance results, and publish or roll back a particular successful build.
The application service checks explicit shop/action grants. REST, MCP, Keycloak
token verification, and a separate queue process now expose this lifecycle;
see [API.md](API.md). Production process supervision remains deployment work.

## Try the workflow

Install the SDK dependencies, build the worker environment, and export the
fictional heat-treatment project:

```sh
uv sync --locked
./scripts/build-worker
uv run python -m examples.export_project > .local/thermal-project.json
uv run python -m chipload.local save .local/thermal-project.json
```

`save` returns the source revision. Substitute that value for `SOURCE_REVISION`:

```sh
uv run python -m chipload.local build thermal-example SOURCE_REVISION
```

This creates a durable build record, runs the source in a container, validates
the returned model in the broker, and runs the nine data-only acceptance cases.
It reports the build ID, status, diagnostics, and test results. A failed build
returns a nonzero CLI exit status. Substitute the successful ID for `BUILD_ID`:

```sh
uv run python -m chipload.local inspect-build thermal-example BUILD_ID
uv run python -m chipload.local publish thermal-example BUILD_ID --expected-generation 0
uv run python -m chipload.local catalog thermal-example
```

Build inspection includes the private artifact. Only `catalog` returns the
public projection. These commands operate on the local database; publication
does not deploy a website or expose a listening network service.

Other authoring commands:

```sh
uv run python -m chipload.local sdk-reference
uv run python -m chipload.local source thermal-example
uv run python -m chipload.local publication thermal-example
uv run python -m chipload.local audit thermal-example
```

The reference includes the actual SDK schemas, supported units, entrypoint
contract, and an example. Source reads include private project files and tests.

Edit the bundle's files or construct a new `SourceProject` in Python. Saving an
existing shop requires `--expected-head SOURCE_REVISION`. A conflict means that
another edit changed the head; read it and reconcile the edits. Saving does not
parse or execute source, so an incomplete draft can be saved.

Build the new revision and inspect its results before publishing it. Every
publication change requires the current generation returned by `publication`:

```sh
uv run python -m chipload.local publish thermal-example NEW_BUILD_ID --expected-generation 1
uv run python -m chipload.local rollback thermal-example ORIGINAL_BUILD_ID --expected-generation 2
uv run python -m chipload.local withdraw thermal-example --expected-generation 3
```

These generations assume the preceding example commands were followed once.
Generations increase on every publication change, including rollback and
withdrawal. A stale generation cannot overwrite a newer publication.
Rollback targets must have been published previously.

## Project and build contracts

`SourceProject` contains a shop ID, `module:function` entrypoint, text files, and
declarative acceptance cases. The zero-argument function returns a `Shop`.
Ordinary imports, modules, functions, and data files work inside the project.
Paths must be relative, cannot traverse directories or shadow the `chipload`
package, and cannot collide with other files. The bundle supports `.py`, `.json`,
`.toml`, `.txt`, and `.md` files. Binary assets are not supported yet.

Limits are 128 files, 128 KiB of characters per file, eight path components,
100 acceptance cases, and 2 MiB for the complete serialized bundle. The total
byte limit also bounds multibyte text. Each case specifies an evaluation request,
time, observations, expected outcome, and optional required reason codes.
Passing these cases establishes conformance to the shop's assertions, not
independent physical qualification.

Source identity includes files, entrypoint, shop ID, and acceptance cases.
File ordering does not affect it. Build identity additionally records the SDK,
dependency lock, Python version, and immutable worker image. The broker rejects
a worker whose SDK fingerprint differs from its own; rebuild the image after
SDK or broker source changes.

The initial environment contains the standard library and locked SDK
dependencies. Builds never install project-supplied requirements or download
dependencies. Operator-provisioned environments for additional pinned libraries
and custom executable evaluation remain to be implemented.

Source may fabricate output or modify its own Python process. The broker treats
all worker output as untrusted: it validates the returned model and references,
enforces the shop ID, computes artifact identities, and runs acceptance cases
outside the source process. Source cannot submit its own test results.

Worker output, saved source, and stored reports use a shared JSON decoder that
bounds nesting and structural work before object allocation. The broker rejects
oversized model collections and evaluation graphs; see
[model admission limits](SHOP_MODEL.md#units-identities-and-artifacts).
Acceptance suites also have an aggregate 1,000,000 work-score budget, counting
each target invocation. Exceeding this budget fails the build without executing
or skipping selected cases. Diagnostics have at most 100 entries, 32 path
components of 200 characters each, and 1,000 characters per message.

These bounds apply outside the source container as well. They do not provide
a separate CPU or memory sandbox for the trusted data-validation process.
Process containment, network-edge controls, filesystem capacity/retention, and
concurrent load qualification remain required before internet deployment.
Authoring/build quotas and dispatch fairness are described below.

## Container boundary

The image build uses a minimal context containing the SDK snapshot, package
metadata, lock file, exported hashed dependencies, and Dockerfile. It excludes
the checkout's `.env`, recovery keys, local databases, and shop source. The base
image is pinned by digest; dependency installation requires hashes and wheels.
The resulting immutable image ID is saved in ignored `.local/worker-image`.

Each source build gets a fresh container with:

- No network, host mounts, Docker socket, equipment credentials, or forwarded
  host environment.
- UID/GID 65532, a read-only root filesystem, all capabilities dropped,
  no new privileges, and Docker's default seccomp profile.
- One CPU, 256 MiB memory with no additional swap, 64 processes, and bounded
  file descriptors.
- A 32 MiB temporary filesystem with `noexec`, `nosuid`, and `nodev`.
- A default 30-second execution limit, 8 MiB protocol output limit, and
  64 KiB diagnostic-output limit.

The broker removes the container on completion or failure. Container logging
is disabled; source output is bounded and is not returned through public
discovery. The Docker broker itself is privileged infrastructure and must remain
separate from a future public API. Its Docker access must never be delegated to
shop source. Containers share the host kernel; production isolation, supervision,
network-edge controls, and deployment validation remain work under the hardening milestone.

These controls use Docker's documented
[runtime restrictions](https://docs.docker.com/engine/containers/run/) and
[default seccomp profile](https://docs.docker.com/engine/security/seccomp/).
The live tests verify configured limits and actual access denials on the test host.

For reproducible data-operation measurements, run:

```sh
uv run python scripts/benchmark-data
uv run python scripts/benchmark-data --large-catalog
```

The synthetic workload has 17 repeated steps and a work score of 94,420 against
the 100,000 limit. The second command adds 1,000 catalog entries, making a
roughly 4 MiB artifact. Each benchmark child has 512 MiB address space, 30 CPU
seconds, and a 60-second parent timeout. Output includes artifact size, Python
and platform versions, median/p95/max timings, and peak process RSS. It checks
the expected supported outcome and rejects an oversized JSON array.

These measurements exclude the database, HTTP, identity verification, and
concurrent load; they are not a deployment throughput claim. In an initial
20-iteration run on the development host (Python 3.14.6, other tests running),
read-and-evaluate medians were about 24 ms for the compact artifact and 380 ms
for the large catalog. Peak process RSS was about 35 MiB and 66 MiB respectively.
The large-model result identified repeated artifact validation as a performance
cost. The repository now caches validated immutable build data and public
catalog bytes under a memory/entry budget. Publication selection and report-byte
hashing still happen on every read; observations and authorization stay live.

Compare the shared published service with and without retention:

```sh
uv run python scripts/benchmark-data --service
uv run python scripts/benchmark-data --service --large-catalog
```

This mode creates a temporary SQLite repository with synthetic trusted fixture
data, then measures the same public evaluation with retention disabled, on the
first load, and with a warm cache. It does not execute the fixture's source or
contact equipment. On the same Python 3.14.6 development host, a 20-iteration
run measured:

| Workload | No-retention evaluation median / p95 | Warm evaluation median / p95 | Warm catalog median |
| --- | --- | --- | --- |
| Compact, 5.8 KiB | 36.5 / 47.8 ms | 20.2 / 24.8 ms | 0.9 ms |
| Large catalog, 4.0 MiB | 872.5 / 910.7 ms | 65.8 / 75.3 ms | 44.2 ms |

The large workload retained two entries with an estimated 10.0 MiB footprint;
peak process RSS, including uncached validation and temporary allocations, was
about 100 MiB. Its first cached load still took about 880 ms. These measurements
include SQLite reads, integrity checks, public projection, and evaluation, but
exclude HTTP, identity verification, and concurrent load. Production
qualification must repeat them in the deployed runtime and exercise cold-load
pressure, eviction, requests from multiple shops, and quotas. See
[API cache policy](API.md#errors-and-limits).

## Storage, authorization, and recovery

The default repository uses `.local/chipload.db` for shop data and
`.local/chipload.db.admission` for capacity policy, policy history, and request
balances. Both files have owner-only permissions. Use `--database PATH` before
the subcommand to select another repository; its capacity store is always
`PATH.admission`. A persistent identity pairs the files. Missing or mismatched
capacity storage stops startup and request admission; it is never silently
recreated with fresh allowances.

This is single-node storage. SQLite transactions preserve source revisions,
build states, publication history, and an audit trail. The separate capacity
store lets request admission proceed during shop writes. For backup or restore,
stop all API, broker, and local writer processes and preserve both databases as
one recovery set. Use [SQLite's backup API](https://www.sqlite.org/backup.html)
for each file while writers remain stopped. Do not copy only the main files
while uncheckpointed [WAL data](https://www.sqlite.org/wal.html#the_wal_file)
exists. Restore both files from the same recovery set with their expected names
and private permissions. Pairing rejects another repository's store, but cannot
detect an older copy from the same repository. The TLS deployment's
[encrypted recovery tool](RECOVERY.md) preserves the pair together with identity,
grants, and TLS material, checks their integrity, and exercises interrupted restore.
Off-host disaster recovery and external secrets storage remain operational work.
Runtime initialization reenables WAL on both restored files without resetting
capacity policy, its history, or request balances.

`ShopService` requires these independent grants:

| Action | Permission |
| --- | --- |
| Read private source | `source:read` |
| Save source | `source:write` |
| Inspect builds and private artifacts | `build:read` |
| Request builds | `build:run` |
| Inspect/change publication, roll back, withdraw | `publication:write` |
| Read the shop audit trail | `audit:read` |

Every grant names a specific shop. Source access does not grant publication, and
one shop's grant does not authorize another shop. Public catalog reads need no
principal and only see an explicitly public, published artifact.

The local CLI assumes the OS user is the operator and constructs that user's
local grants. It does not perform remote authentication. The network transports
construct principals from verified identities and trusted grants; accepting a
principal from request JSON would bypass authorization.

Publication requires a successful build with at least one passing acceptance
case. Failed builds and draft edits leave the published revision intact.
Public reads select the publication in a current database snapshot and may reuse
validated immutable data from the bounded cache. Withdrawal removes access for
subsequent reads; an already completed
response cannot be recalled. HTTP reads require revalidation and check withdrawal
before returning 304; private and evaluation responses use `no-store`.

Build claims have deadlines. A late worker cannot complete an expired claim.
After a broker interruption, run:

```sh
uv run python -m chipload.local recover-builds
```

Recovery fences expired claims, removes their named worker containers, and
records failure without re-executing source. An unexpired build is left alone.
If container removal fails, recovery raises an error and the job remains
unpublishable. The separate `python -m chipload.broker` queue process performs
recovery continuously. The local CLI also supports explicit operator recovery.

## Authoring and build quotas

Platform capacity policy is stored in the paired capacity database and applies below REST,
MCP, and the local shop services. A verified identity's usage spans its shops.
Shop business terms remain in the Python shop model.

| Limit | Deployment | Per shop | Per identity |
| --- | --- | --- | --- |
| Pending builds, including running builds | 100 | 10 | 20 |
| Running builds across all brokers | 2 | 1 | 1 |
| Accepted builds in the preceding hour | 600 | 60 | 120 |
| Retained build records | 10,000 | 1,000 | 2,000 |
| Stored/reserved build report payloads | 2 GiB | 256 MiB | 512 MiB |
| Retained source revisions | 10,000 | 1,000 | 2,000 |
| Source payloads | 1 GiB | 128 MiB | 256 MiB |

An accepted job reserves the full 8 MiB report allowance until it finishes.
The finished report's actual bytes replace that reservation; failures still
consume their actual storage and count toward the hourly/retained-build limits.
Expired running jobs retain their slots and reservations until their containers
have been removed and failure recorded. Quotas do not silently discard source,
reports, publications, rollback history, or accepted work.

Source/build usage checks, admissions, and dispatch claims use a
[SQLite write transaction](https://www.sqlite.org/lang_transaction.html) in the
shop database. Each reads a committed policy snapshot from the capacity store;
an admission already in progress may finish under its earlier policy when the
operator changes limits. Separate API/broker processes cannot race past a shared
usage limit. Dispatch selects an
eligible shop with the oldest dispatch turn, then identity turn and queued
time/ID. New queues start at the current turn so creating new shops does not
jump ahead of waiting queues. This is fairness of job dispatch, not equal CPU
time: jobs have their own execution limits. Each broker executes one job at a
time; a larger running limit only helps when additional brokers are deployed.
The trusted local single-build command may select a specific job but still
enforces running capacity.

Only the local operator can inspect or change platform quotas; no shop grant,
REST operation, or MCP tool grants that authority:

```sh
uv run python -m chipload.local --database .local/chipload.db quotas
uv run python -m chipload.local --database .local/chipload.db quotas \
  --shop thermal-example --identity KEYCLOAK_SUBJECT
```

Output contains the policy generation, the complete `policy` object, deployment
usage, and requested shop/identity usage. Policy/request balances and shop/build
usage are read in separate snapshots, not one transaction across both files.
Save an edited **policy object only**
to `.local/quota-policy.json`, then apply it using the generation just read:

```sh
uv run python -m chipload.local --database .local/chipload.db set-quotas \
  .local/quota-policy.json --expected-generation 0
```

Updates are recorded, guarded against concurrent edits, and visible to all
processes on their next admission/claim. Lowering a quota blocks new admissions
as necessary; it does not cancel or erase existing work. A no-op source save
does not create another revision or audit event. Reusing existing source or
build request identities does not double-charge admission.

The policy's `requests` object controls shared HTTP and mutation rates; see
[request admission](API.md#request-admission). Request allowances count retries
and no-op calls, independently of source/build storage and job quotas. Deployment
usage also reports the number of retained request buckets.

Opening schema 0–4 upgrades the shop database to schema 5 and creates the paired
capacity store. Migration first commits a pending marker, copies policy,
history, and request balances, then marks the pair ready and removes the old
tables. Interrupted startup resumes that copy before serving requests. Existing
limits, policy generations, and spent allowances are preserved. Older stores
without request policy gain defaults. Build requester identities are recovered
from request audit events, falling back to source ownership or a shared `legacy`
identity when necessary. Existing usage may exceed defaults and is preserved.
Before upgrading, stop old API/broker processes and make a consistent backup.
Do not run old code alongside the new schema. SQLite remains single-node
storage; this does not establish safe use over a network filesystem or across
independent repositories.

Payload quotas exclude SQLite/WAL/index overhead, observation snapshots, and
audit/publication history. Network-edge controls, filesystem quotas,
and tested retention/backup/restore procedures remain required for
internet deployment. Reaching a retained-data limit requires deliberate
operator capacity or retention work; automatic history deletion is not provided.

## Verification

```sh
uv run --extra server python -m unittest discover -s tests -v
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_worker_live.py' -v
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_quota_worker_live.py' -v
```

Live worker tests require `./scripts/build-worker` first. They cover multiple
modules and data files, reproducible model/build identities, actual host and
network access denials, resource configuration, looping and flooding source,
forged model output, diagnostics, and publication through repair and rollback.
They use temporary databases and remove only the worker containers they create.
The queue test starts two real brokers: one holds a looping shop worker while
the other builds and publishes a different shop. It then stops only the test's
looping container and checks that its reservation is released normally.
