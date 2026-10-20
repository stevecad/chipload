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
quotas, and deployment validation remain work under the hardening milestone.

These controls use Docker's documented
[runtime restrictions](https://docs.docker.com/engine/containers/run/) and
[default seccomp profile](https://docs.docker.com/engine/security/seccomp/).
The live tests verify configured limits and actual access denials on the test host.

## Storage, authorization, and recovery

The default database is `.local/chipload.db`, opened with owner-only permissions.
Use `--database PATH` before the subcommand to select another database. SQLite
transactions preserve source revisions, build states, publication history, and
an audit trail. This is single-node storage. Live backup must use SQLite's backup
mechanism, not an uncoordinated copy of the main file while WAL writes continue.

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
Public reads select the publication in one database snapshot and use no process
cache. Withdrawal removes access for subsequent reads; an already completed
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

## Verification

```sh
uv run --extra server python -m unittest discover -s tests -v
CHIPLOAD_WORKER_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_worker_live.py' -v
```

Live worker tests require `./scripts/build-worker` first. They cover multiple
modules and data files, reproducible model/build identities, actual host and
network access denials, resource configuration, looping and flooding source,
forged model output, diagnostics, and publication through repair and rollback.
They use temporary databases and remove only the worker containers they create.
