# Continuous integration

[Checks](../.github/workflows/checks.yml) runs on pushes to `main`, pull requests,
and manual dispatch. Each job uses a fresh GitHub-hosted Ubuntu 24.04 runner.
Ordinary tests, lint and executable technical/business examples run on Python
3.12.15 and 3.14.6 with the locked server dependencies.

After both Python jobs pass, five jobs use Docker Engine 29.9.2, build the
container images from the checkout and run disposable integration fixtures:

| Profile | Coverage |
| --- | --- |
| `python` | Source builds, hosted Python and SDK isolation, MCP authoring, REST discovery, runtime persistence, binary assets, Python constraints and assessments, ERP observations, evidence revocation, alternative offers, supplier listings, and mediated network queries without sockets or connector credentials. |
| `tls` | Verified HTTPS and Keycloak, external client boundaries, callable operations, and direct peer identity/publication queries. |
| `recovery` | Encrypted shop/identity recovery, agent/database credential rotation, and signing-key rotation and revocation. |
| `federation` | Separate cloud/connector containers, real Keycloak/OpenBao enrollment, three Python shops, REST/MCP supplier selection, business-rule rejection, withdrawal/expiry and direct dispatch during cloud outage. |
| `secrets` | TLS OpenBao bootstrap, recovery and the reviewed older-image upgrade, workload credentials, Keycloak operator identity, independent emergency quorum access, HTTPS adapters, and the contained executor. |

[`scripts/check-live`](../scripts/check-live) defines the exact suites and enables
their prerequisite flags before importing tests. Missing suites, skipped checks,
expected failures and test failures all fail the selected profile. The ordinary
test command intentionally skips opt-in integration tests; its success alone
does not establish container coverage.

The TLS lifecycle and operation fixtures use a bounded transport retry when the
server explicitly refuses MCP admission with `request_capacity`, a null request
ID and matching retry metadata. They preserve the exact request and durable key.
Connection failures, lost replies, application errors and arbitrary HTTP 503s
are not retried. Dedicated tests verify those distinctions and the retry bound.
Load benchmarks use their ordinary transports and continue to count refusals.

Connector fixture reads also allow four bounded attempts after an explicit
HTTP 503 `request_capacity` refusal with valid retry timing. Concurrent
publication/polling can legitimately contend for the admission-store writer.
A real SQLite-lock regression verifies that refusal and recovery. Relay failures,
lost replies, other status codes and invalid retry metadata fail immediately;
production clients and mutation retry behavior are unchanged.

To run the same profiles locally, install Docker Engine 29 with Compose, GnuPG 2.4+,
Python 3.12+ and `uv`, then run:

```sh
uv sync --locked --extra server
./scripts/build-worker
./scripts/build-runtime
./scripts/build-tls
./scripts/build-secrets
uv run --extra server python scripts/check-live python
uv run --extra server python scripts/check-live tls
uv run --extra server python scripts/check-live recovery
uv run --extra server python scripts/check-live secrets
uv run --extra server python scripts/check-live federation
```

Keep the checkout and image fingerprints fixed during each run. These profiles
create and clean up their own temporary services, storage and credentials. They
do not use the persistent development stack or contact equipment. Docker access
is required; use a disposable development or CI host for untrusted contributions.

The hosted jobs stop the runner's preinstalled daemon and start the pinned engine.
Chipload's runtime resolves its local Unix socket from the Docker environment so
the broker and host tools use the same daemon. Keep these jobs on disposable hosted runners. The external
peer test depends on Docker 29's [bridge routing behavior](https://docs.docker.com/engine/release-notes/29/):
older engines can proxy both clients through one address, which correctly shares
their request allowance but cannot verify independent client budgets.

The workflow grants only repository-content read access, disables persisted Git
credentials and dependency caching, and uses full commit pins for third-party
actions plus a version and checksum for `uv`. It does not consume repository
secrets, publish images, deploy services or upload fixture state. This follows
[GitHub's action security guidance](https://docs.github.com/en/actions/reference/security/secure-use)
and the [uv integration guide](https://docs.astral.sh/uv/guides/integration/github/).
Updating a pin requires reviewing its upstream release and rerunning the checks.

CI covers a selected integration set. The complete process source-build matrix,
CadQuery dependency environment, ACME issuance and user-systemd scheduling,
additional recovery/fairness drills, and load benchmarks have separate commands
in `AGENTS.md` and their feature documentation. Run the relevant checks when
changing those areas. A passing workflow does not establish site-specific
internet deployment, equipment qualification or third-party connector support.
Required branch checks and repository protection are separate GitHub settings;
this workflow does not configure them.
