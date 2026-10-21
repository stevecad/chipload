# Application containers

The API and build broker can run in separate containers on a single Linux
Docker host. They share the repository's two database files. The API serves
REST and MCP; the broker starts the existing isolated source workers through
the host Docker daemon.

This packaging adds process and filesystem limits. The supplied Compose file
exposes HTTP only on host loopback. The [TLS stack](TLS.md) adds a gateway,
production-mode Keycloak, and private application/identity networks. Production
OpenBao lifecycle, automatic certificate renewal, off-host recovery,
and deployment load qualification remain required for public deployment.
The separate [executor packaging](ADAPTERS.md) supports offline simulation and
HTTPS calls with scoped OpenBao credentials.
The TLS deployment has [operator-driven credential and signing-key rotation](CREDENTIALS.md).

## Build and configure

Use Python 3.12+, `uv`, Docker Engine, and Docker Compose on the same host as
the data directory. Build worker, API, broker, and executor images from the same checkout:

```sh
./scripts/build-worker
./scripts/build-runtime
cp deploy/runtime/settings.example.json .local/runtime.json
chmod 600 .local/runtime.json
```

The builders copy only SDK source, license notices, the package/lock files,
exported requirements, and the relevant Dockerfile into temporary build contexts. Repository secrets,
shop projects, local databases, and `.env` files are excluded. Python and Docker
CLI base images are pinned by digest. Installation accepts hash-locked binary
wheels. Image IDs are saved in `.local/worker-image`, `.local/api-image`, and
`.local/broker-image`, plus `.local/executor-image` for the separate executor.
Startup checks image roles, SDK fingerprint, and dependency lock against the
checkout. Rebuild these images after SDK/lock changes.
Images carry Chipload's MIT license. The broker also includes the Docker CLI
29.9.1 [license](https://github.com/docker/cli/blob/v29.9.1/LICENSE) and
[notice](https://github.com/docker/cli/blob/v29.9.1/NOTICE) under
`/usr/local/share/licenses/`; Python wheels retain their package metadata.

Edit `.local/runtime.json` before startup. Keep `database` set to
`/data/chipload.db`. Set `resource_url` to the exact public MCP URL and configure
the Keycloak issuer, JWKS URL, and explicit identity/shop bindings as described
in [the API guide](API.md#keycloak-identity-and-grants). The issuer and JWKS must
be reachable from inside the API container. The example uses production HTTPS
URLs; it does not provision those endpoints or certificates.

For loopback HTTP access, use `mode: development` and
`resource_url: http://localhost:8088/mcp`, with a reachable HTTPS Keycloak issuer
and JWKS URL. The local Keycloak started by `scripts/dev-stack` listens on host
loopback; `localhost` inside the API container addresses the API container
itself. The existing host-runtime walkthrough remains available for that local
identity stack. Use the [TLS identity/application topology](TLS.md) to run these
services together in containers.

Production mode requires HTTPS endpoint URLs. A TLS reverse proxy must preserve
the configured Host header and forward to host loopback port 8088. Forwarded
headers are ignored by default. The TLS stack configures an exact trusted gateway
IP and requires its replacement client-address header for peer budgets; see
[request admission](API.md#request-admission). Do not expose the
container's plain HTTP listener directly to the public internet.

## Run and stop

Run as the non-root account that owns the configuration and data:

```sh
./scripts/runtime up
./scripts/runtime status
./scripts/runtime logs
./scripts/runtime down
```

`up` creates `.local/runtime-data/` with mode 0700 and mounts it at `/data` in
both services. The data directory must contain only Chipload database/journal
files; unrelated files and symlinks are rejected before mounting. Configuration
must be an owner-only regular file. Containers
use the caller's numeric UID/GID so the operator can also use the local CLI
against `.local/runtime-data/chipload.db`. Both databases and their WAL files
remain private. The launcher reads the local Docker context's Unix socket and
gives only the broker its socket group. Remote Docker endpoints are rejected
because bind paths and SQLite storage must be local to this host.

Options `--config`, `--data`, `--port`, and `--name` select another configuration,
data directory, loopback host port, or Compose project. Use the same options for
all commands managing that instance. A changed host port also needs the matching
development `resource_url`; production URLs describe the external TLS endpoint.
The API always listens on container port 8088. `down` preserves the bind-mounted
data and does not delete shop history.

API health checks use the configured Host header and the same bounded request
admission as other HTTP calls. The broker has no incoming listener. A process
exit triggers its configured restart policy; Docker health status alone does
not restart a process. Inspect broker logs and queued build states when
investigating a stalled queue. On graceful shutdown the API has 15 seconds to
finish requests; the broker gets 45 seconds to finish its current bounded build.

Back up and restore the shop and `.admission` databases together using the
[storage recovery procedure](AUTHORING.md#storage-authorization-and-recovery).
For the complete TLS/Keycloak deployment, use the [encrypted recovery tool](RECOVERY.md).
Before upgrading a running deployment, stop its API and brokers and take a
consistent backup; schema migration is not a rolling-upgrade protocol.

## Enforced boundaries

| Process | Storage and access | Runtime limits |
| --- | --- | --- |
| API | Shop databases and read-only settings; no Docker socket or CLI; outbound network for JWKS | Non-root, read-only image, no Linux capabilities, no privilege escalation, 1 CPU, 1 GiB memory with no additional swap, 128 processes |
| Broker | Shop databases, read-only settings and Docker Unix socket; no IP network or OpenBao credentials | 1 CPU, 512 MiB memory with no additional swap, 128 processes; same filesystem/capability restrictions; starts separately constrained workers |
| Python SDK service | Only paired repository storage, mounted separately for each Python run; no network, settings or Docker socket | Repository owner's non-root UID/GID, read-only image, 1 CPU, 256 MiB memory/address space, 64 file descriptors, CPU/wall deadlines |
| Source worker | Stdin source bundle, bounded temporary storage; no host mounts or network | Existing worker boundary: non-root, 1 CPU, 256 MiB, 64 processes, execution/output deadlines |

The selected worker may include [pinned shop libraries](DEPENDENCIES.md).
Source and Python code use the shop interpreter; the SDK service explicitly uses
the core interpreter. Runtime admission verifies the SDK and core dependency
fingerprints while permitting the separately identified shop dependency set.
Public API, broker and executor images keep their own core environments.

API and broker `/tmp` mounts are limited to 32 MiB with `noexec`, `nosuid`, and
`nodev`. Core dumps are disabled. Container logs rotate with a 10 MiB size and
three-file limit. The API image contains no Docker client. Equipment secrets
and the OpenBao bootstrap files are mounted into neither service.

The API dispatches domain operations to two [supervised service processes](SERVICE_WORKERS.md),
each with a 256 MiB address-space limit and CPU/wall deadlines. These trusted
processes share the API's filesystem and network namespace. The 1 GiB container
budget covers the HTTP supervisor, both service processes and overhead. The
default 32 MiB artifact cache allowance is divided between the workers. Startup
requires Linux process-descriptor support; the shipped image and live tests
exercise that facility under the container restrictions.

The broker is trusted infrastructure: access to the Docker socket permits host
control even when the socket bind mount is marked read-only. Its process limits
do not restrict that daemon authority. The public API and untrusted source
workers must never receive this socket. A dedicated worker host and restricted
control channel remain part of production deployment design.

## Verify

```sh
CHIPLOAD_RUNTIME_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_runtime_live.py' -v
```

The live test launches the shipped Compose file in a randomly named project
with temporary storage. An MCP client submits broken source, reads diagnostics,
repairs it, builds and publishes through the broker, updates and rolls back.
A REST client checks discovery and permissions, then withdraws the publication
after both services restart. A separate broker restart during an active build
verifies graceful completion. The test inspects actual container settings and
checks filesystem denial, missing API Docker access, broker network denial,
and private database permissions. It serves temporary public signing keys
inside the API's network namespace; the signer stays outside the containers.
That fixture tests JWT verification and container networking without standing
in for production Keycloak/TLS qualification. Separate live identity tests
exercise actual Keycloak tokens and OpenBao access denials.

Only test-owned containers and temporary data are removed. The SDK's existing
worker and lifecycle live tests remain required when those boundaries change.

The broker also processes [Python automation runs](PYTHON_API.md) in separate restricted workers. It mounts `/etc/chipload/settings.json` read-only to recheck explicit grants before each SDK call, then sends the scoped call to a separate SDK service container. Compose supplies the host data directory through `--service-data`; only repository files may occupy that directory. Code and build loops have separate capacity; code cannot inherit the broker's filesystem or Docker socket. On shutdown or recovery, both the code and SDK service containers must be removed before the run settles; completed SDK calls remain committed.
