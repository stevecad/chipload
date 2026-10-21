# Threat model

This describes the implemented single-host deployment and its remaining security
work. Listeners default to host loopback; explicit client IPv4 binding is available
with supplied deployment certificates, while administration stays local. Public internet qualification
is an open milestone in [the roadmap](../ROADMAP.md). The model assumes the
operator controls the host, Docker daemon, configuration and image build process.
A compromised operator or host can read and change shop data and credentials.

The [deployment design](../ROADMAP.md#self-hosted-and-hosted-deployments) supports
self-hosted shops and provider-operated instances. Its initial hosted boundary
is a separate VM for each independent customer organization, including the
broker, Docker daemon and private state. Multiple shops within one instance have
application authorization boundaries; they share trusted services and the host
kernel. Separate Compose projects do not protect against compromise of that
host. This recommendation follows the authority exposed by the current broker
and Docker's [daemon security model](https://docs.docker.com/engine/security/#docker-daemon-attack-surface).
VM isolation, provider administration and inter-VM network policy still need
verification on the hosting platform; existing container tests do not prove them.

## Assets and callers

Protect private shop source, evidence and observations; the integrity and history
of published capability/business claims; authorization bindings; credentials;
durable execution records; and availability of discovery and authoring.
Equipment effects and ERP records have authority outside Chipload. A simulation,
successful Python build or supported capability evaluation is not physical
qualification or confirmation that an external action occurred.

Anonymous clients can query an explicitly published projection. Authenticated
clients receive only the explicit shop/action/operation grants attached to their
verified identity. Treat both client input and shop-authored Python as untrusted,
including data produced by successfully executed code. A legitimate shop agent
may still supply malicious or accidentally expensive source and data.

Published supplier listings disclose explicit semantic input mappings and may
reference a site's deliberately supplied public coordinates. Public catalog
visibility and directory indexing are separate opt-ins. Validation rejects
public mappings to hidden catalog items and private target/site references.
Listing queries require a public revision and use the same publication,
observations, evidence and private-condition policy as ordinary evaluation.
Unknown bindings are not ignored. `test_listings.py`, `test_listing_transports.py`
and `test_listing_worker_live.py` exercise these boundaries.

An operator-configured instance ID enables typed public peer queries over the
existing API boundary. Replies identify the instance/request and publication
generation/revision. Trusted direct clients use HTTPS processes with CPU,
memory, byte and wall limits covering DNS/TLS/reads. All resolved addresses are
validated and pinned while the original TLS hostname is verified. Private-origin
exceptions belong to operator policy. `test_peers.py` and
`test_federation_wire.py` exercise actual TLS, deadlines, generation fences and
hostile replies. Serving this route adds no outbound peer authority to the API.
The directory, connector and relay in [the federation design](FEDERATION.md)
remain to be implemented.

## Boundaries

| Component | Authority and boundary |
| --- | --- |
| TLS gateway | Terminates verified HTTPS; replaces the client-address header; forwards to the private API listener. Client binding is explicitly configured and defaults to loopback. Administration remains on loopback. |
| API supervisor | Verifies identity, applies ingress budgets and parses bounded requests. It can access its databases and read-only settings, and contact JWKS. It has no Docker socket or equipment credential. |
| Service workers | Trusted Chipload code with the API container's database, filesystem and network authority. CPU, address-space, wall-time and IPC limits contain resource failures. They do not execute shop Python. |
| Build/Python broker | Trusted infrastructure with database access, read-only grant settings and the Docker socket. It has no IP network. Docker daemon access grants host authority; container restrictions do not remove that authority. |
| Source and Python workers | Untrusted code in disposable containers with no network or host mounts. The broker validates artifacts; hosted Python SDK calls travel through bounded mediation using the caller's admitted grants intersected with current settings. |
| Python SDK service | Trusted domain implementation in one separate container per Python run. It has database authority but no Docker socket, settings, secret mounts or network. CPU/memory/wall limits, cancellation and recovery bound its work; shop Python never executes there. |
| Operation executor | Separate trusted adapter process/container with one pinned binding and restricted network access. A live adapter obtains its own scoped OpenBao credential. It cannot infer permission from Python source alone. |
| Keycloak / OpenBao | External identity and secret authorities. Chipload validates identity claims and enforces its own bindings. Adapter secrets are confined to the configured executor and receiver path. |
| Recovery tools | Operator-only host commands with private deployment state and encrypted archives. They are not shop REST or MCP operations. |
| ACME issuer | Pinned non-root client with its own private writable account storage, read-only configuration, explicit HTTPS trust and a temporary HTTP challenge listener. It has no application data or Docker socket. The separate host delivery runner has operator/Docker authority; user-systemd filesystem restrictions may be unenforced. |

The shop and admission databases form one durable repository. The admission store
separates request-accounting writes from shop writes; it is not a security boundary
against a compromised API, broker or executor. Service processes also do not add
a filesystem or network namespace inside the API container.

## Threats, controls and evidence

| Threat | Implemented controls | Verification |
| --- | --- | --- |
| Cross-shop access or excessive authority | Verified issuer/audience/signature and explicit shop/action grants; operation ID restrictions; public projection; current grant intersection on hosted SDK calls | `test_auth.py`, `test_transports.py`, `test_programs.py`, `test_execution_transports.py`, actual Keycloak/TLS lifecycle tests |
| A password-only token obtaining managed secrets-operator access | Version-2 operator clients require enrolled OTP, a dedicated required password/OTP flow, signed AMR checks and an independent OpenBao OTP claim binding; refresh issuance disabled; explicit journaled upgrade for older receipts | `test_operator_mfa.py`, `test_secrets_identity.py`, real browser factors, direct OpenBao denial, interrupted upgrade and restored login in `test_secrets_identity_live.py` |
| Public disclosure of private claims or stale publication | Explicit projection, publication selection from the database on every request, integrity-bound immutable caches, uncached observations and permissions | `test_published.py`, `test_cache.py`, process/transport parity tests |
| Source execution obtaining host data, network or credentials | Separate non-root containers, no host mounts/network, read-only image, restricted temporary storage, memory/CPU/process/output limits | `test_worker_live.py`, `test_program_live.py` |
| Shop dependency code executing with database authority | Separate shop Python environment; explicit core interpreter for SDK services; binary hash-pinned installs and unchanged core package constraints; no shop interpreter invocation after installation during image build | `test_worker_dependencies.py`, `test_worker_dependencies_live.py`; [environment contract](DEPENDENCIES.md) |
| Forged or expensive worker output | Broker-side decoding and model validation; JSON, collection, graph, diagnostic and evaluation budgets | `test_data_limits.py`, hostile-output live worker tests |
| Project assets escaping their directory, changing unnoticed or exposing private bytes | Canonical base64, bounded decoded/project size, combined path/count checks, immutable source revisions and private source reads; materialization in fresh source-container directories with no automatic extraction or format parsing | `test_project_assets.py`, actual source builds, transport privacy and assessment update/rollback in `test_project_assets_live.py` |
| Python constraint answers bypassing static rules or evidence | Functions run only in source workers; the broker requires exact check/case coverage, binds answers to the build/query/time and recomputes domain outcomes. Build-test answers never substitute for a new query. | `test_python_checks.py`, actual conditional/rejected inputs, execution failures and containment in `test_python_checks_live.py` |
| Queued queries leaking private conditions or returning a superseded answer | Separate evaluation grant, caller-owned receipts, exact published source/environment, no function execution for opaque public contracts, and publication/observation/revocation checks before returning receipts | `test_assessments.py`, REST and MCP queries through real source containers in `test_assessments_live.py`, receipt preservation in the recovery drill |
| Request floods or expensive data operations | Shared peer/identity/shop quotas, bounded admission state, deterministic work budgets, two supervised API service workers and container limits | `test_request_limits*.py`, `test_quotas.py`, `test_service_pool.py`, `benchmark-service` |
| Replayed mutations, lost responses or ambiguous external effects | Expected revisions/generations, durable request keys and call journals, claim fencing, no automatic code/service replay, retained uncertain operation locks, receiver reconciliation | `test_lifecycle.py`, `test_programs.py`, `test_service_pool.py`, `test_execution*.py`, adapter fault tests |
| Outbound-request abuse or credential leakage | Untrusted containers have no network; operator-owned adapter bindings; pinned HTTPS trust/receiver path; bounded network work; no redirects, proxy inheritance or automatic dispatch retries; scoped AppRole/KV access | `test_remote_adapter.py`, real OpenBao and TLS receiver denials in `test_remote_adapter_live.py`, contained executor tests |
| Spoofed client addresses or failed identity trust | Exact trusted gateway address, gateway-replaced single client header, disabled Uvicorn proxy rewriting, CA/hostname verification and bounded JWKS caching | `test_proxy.py`, `test_auth.py`, `test_tls_live.py`, distinct real network clients and private-port denials in `test_edge_live.py` |
| Invalid certificate delivery or interruption leaving mixed TLS files | Offline validation before mutation, private durable candidate/journal, configuration and storage checks on retry, startup/backup interlocks, mounted-file recreation and exact live leaf-fingerprint verification | `test_certificates.py`, installer process death and CA replacement in `test_tls_live.py`; receipt preservation in `test_recovery_live.py` |
| Renewal overwriting manual changes, losing its account or bypassing client restrictions | Explicit deployment binding and CA trust, account-key receipts, retained issuer container, actual image/mount/network/resource checks, guarded installation ownership, durable disable and recovery intent | `test_acme.py`; real challenges, CA failure, account loss, Docker policy drift, process death and a systemd timer in `test_acme_live.py` |
| Issuer recovery losing its account or overwriting another deployment | Authenticated encrypted snapshots, bounded private reconstruction, stopped-client and storage checks, atomic publication without replacement, old-container refusal, disabled delivery and excluded timer paths on restore | `test_acme_recovery.py`; actual backup process death, original-state deletion, preserved account/registration and renewed live certificates in `test_acme_recovery_live.py` |
| Recovery resetting authority, history or spent allowances | Paired database backups, encrypted authenticated archives, preserved identity state, fresh restore destinations, durable incomplete/rotation markers and operator locks; separate offline OpenBao recovery preserves quorum, cluster identity and policies | `test_recovery*.py`, `test_credentials*.py`, `test_secrets_recovery*.py` |
| Database checks breaking concurrent write coordination | Metadata-only checks of existing files, serialized exclusive creation, SQLite-managed descriptors and durable transactions | `test_storage.py` probes actual locks from another process; admission and lifecycle concurrency tests |
| Unconfirmed container cleanup releasing worker capacity | Terminal builds require confirmed worker removal or inert name reservation; failed cleanup retains running slots and report reservations; Python cleanup attempts both containers within one bounded budget | `test_container_cleanup.py`, lifecycle quota/recovery regression, actual retained-worker and delayed-removal tests |
| Delayed creation after recovery observes an absent container | Durable creation intent checked against the current claim; one attempt per name; unresolved attempts retain capacity; acknowledged containers start by immutable ID | `test_launches.py`, paused build/SDK/code creation and late-container recovery in live tests |
| Operator recovery releasing capacity before delayed requests are fenced | Durable inert name reservations, current-protocol admission, cleanup by immutable ID, and reservation verification/reinstallation before broker execution | `test_launch_reservations.py`, actual CLI with paused creators/removers, cancellation and restored-store startup in `test_launch_reservations_live.py` |

These tests establish behavior for their stated fixtures and failure cases. They
do not demonstrate resistance to every kernel, interpreter, dependency or
distributed denial-of-service attack. The broker validates build output within
its own container budget. Hosted SDK service calls run in separate containers;
the broker retains grant checks, admission, call journals and Docker supervision.
Those service containers still have authority over the entire mounted repository,
so their trusted implementation must enforce the caller's shop/action grants.

Shop-owned evidence revocations have a separate action grant and durable receipt.
They are read with the current publication and observations, excluded from every
capability route, and rechecked for operation admission and dispatch. Source
rollback cannot restore a revoked evidence ID. Private notes stay behind evidence
permissions. This does not independently verify a shop's declarations or stop
already-dispatched work. Restoring an older backup requires reconciliation of
revocations recorded after that snapshot.

## Dependency audit checkpoint

The [2026-10-21 Python audit](security/python-audit-2026-10-21.json) queried OSV
for 77 locked PyPI package/version pairs, including the optional CadQuery lock,
and returned no matching advisories. That query does not cover native libraries
or operating-system packages. The [image audit](security/image-audit-2026-10-21.json)
records Trivy 0.76.1's database time, exact image identities, input hashes,
severity counts and remaining high/critical matches. These are version matches
requiring exposure review, not confirmed exploitable paths or release clearance.

The gateway now builds Caddy with patched Go and HTTP/2 dependencies and installs
the pinned zlib fix. Its remaining scanner match is GO-2026-5932: the build checks
the actual package graph and refuses any deprecated `x/crypto/openpgp` package.
The Docker CLI now uses Go 1.26.10 as well. See [the gateway build](TLS.md).
The [base-package review](security/base-package-review-2026-10-21.json) records
commands and observations for the exact core images. The affected Perl
`Archive::Tar` module and `systemd-homed` service are absent. The reported
util-linux paths require privileged mount or namespace operations excluded by the
supplied container policy. The affected `infocmp` utility is present, with no
trusted application invocation identified; untrusted shop code can already run
utilities within its container. The [libacl caller review](security/acl-caller-review-2026-10-21.json)
identifies five installed utilities importing the affected pathname functions.
Trusted-module import snapshots did not load libacl, and the reviewed runtime
paths invoke Python or Docker, with no trusted ACL call identified. The vulnerable
package remains installed and its scanner finding is retained. These observations
do not cover every dynamic service path or optional dependency image. Revisit the
conclusions when images, dependencies, launch policies or trusted subprocess paths
change; they do not reduce the broker's Docker authority.

The optional geometry image now includes the Debian security snapshot and the
Expat `2.9.0-1~deb13u1` fix; see [dependency maintenance](DEPENDENCIES.md).
The image report retains outstanding matches in base utilities, private
identity/secrets infrastructure, the certificate client, other optional geometry
libraries and the test CA. No scanner findings are suppressed. Remaining fixes
and exposure review are still required before release clearance.

The [Go binary audit](security/go-binary-audit-2026-10-21.json) uses govulncheck
1.9.0 on binaries copied from the pinned images without executing them. It records
tool provenance, database time, binary hashes, matched symbols and comparison
with the image scan. PostgreSQL's reported high/critical Go standard-library
matches are in its `gosu` helper, with no matching vulnerable symbols found.
A separate user/group parser finding is present in that helper; the supplied TLS
deployment runs PostgreSQL as `postgres`, bypassing the entrypoint's root-only
`gosu` branch. This conclusion does not cover altered startup or manual helper use.
The original issuer and OpenBao have named-symbol matches; the audit retains that
baseline. The rebuilt [issuer](ACME.md) uses the patched Go/HTTP dependencies and
native packages. Its only remaining scanner finding is the OpenPGP wildcard
advisory; the build verifies that none of those packages are compiled. The audit
separates wildcard records from named symbols and records the final binary hash
and package graph check. The rebuilt [OpenBao service](SECRETS.md) uses Go 1.27.3,
`x/net` 0.60.0 and the zlib fix, preserving its upstream release metadata for
scanners. Its thirteen previous named-symbol matches are absent in the updated
binary; the remaining version-based OpenPGP finding concerns packages excluded
from compilation. The audit verifies the source archive against upstream plus
the committed module locks. Startup verifies build labels and passes an immutable
image ID to Compose. These results do not clear the other image findings or the
remaining operational qualification. Named-symbol presence alone does not
establish reachability from a shop caller.

## Remaining deployment work

- Qualify a public edge: exposure, TLS renewal, traffic policy, sustained/adversarial
  load, tenant fairness, operational latency targets and alerting. The
  [deployment benchmark](TLS.md#measure-the-deployed-path) adds routed clients,
  TLS/JWT, business queries and simultaneous Python authoring to the component
  measurements. It counts actual correct results, refusals and missed scheduled
  requests and enforces its declared latency targets in strict mode. The added
  second-shop fixture sends valid REST/MCP queries against a larger catalog and
  workflow. Faster JSON scanning reduced contention, but its recorded run still
  exceeded the reader latency target and returned two capacity refusals. These
  fixtures do not establish WAN capacity or independent tenant fairness;
  availability under costly valid discovery traffic remains unqualified.
- Reduce worker-host authority and strengthen isolation for hostile tenants.
  The broker's Docker socket remains a host-level trust dependency. Shop-specific
  dependency environments are operator-selected and hash-pinned. Python constraints
  run during isolated builds and queued assessments with shared admission,
  cancellation and publication checks. Qualifying isolation for hostile tenants
  remains deployment work. The initial hosted design avoids sharing this Docker
  host between unrelated customer organizations; denser hosting requires a
  separately qualified execution boundary.
- Qualify maintenance for indefinitely ambiguous legacy launches and eventual
  retirement of inert name reservations. The operator command handles the new
  cleanup protocol; older removal requests by name could delete a reservation
  and are refused. Preserve reserved containers and their images, exclude them
  from pruning, and stop old writers before restore. Absence or elapsed time
  alone cannot prove that every old daemon request has ended.
- Qualify production OpenBao operator enrollment, authenticator custody and emergency access,
  audit retention and replacement-host recovery. The separate [private secrets
  service](SECRETS.md) now supplies TLS, PGP-encrypted threshold initialization,
  explicit unsealing, audit storage and certificate reload. Its tests verify
  actual AppRole denials and sealed restart. New operator grants require an
  enrolled authenticator and password/OTP login, with OTP evidence enforced by
  OpenBao. Version-1 receipts require the explicit `enable-mfa` upgrade; this does
  not qualify identity proofing, master-realm administration or phishing resistance.
  The [independent emergency procedure](SECRETS_EMERGENCY.md) uses a separate
  protected password for a five-minute login limited to root-generation ceremony
  endpoints and self-token inspection/revocation. Generating root still requires
  the unseal quorum and encrypts the result to the agreed PGP recipient. The live
  drill has no Keycloak instance and verifies fresh login after cold restore.
  The password can disrupt ceremonies if compromised; root tokens do not expire
  automatically. An absent completed attempt or cancellation does not establish
  token revocation. Independent custody and incident reconciliation remain
  operational requirements.
  Encrypted offline backup/restore is
  tested after deleting the original store, including quorum, cluster identity,
  revoked root-token and workload-policy preservation. This covers fresh storage
  on the same host; replacement-host and custody arrangements need qualification.
  An [explicit image transition](SECRETS_UPGRADES.md) upgrades the original pinned
  2.7.1 service through encrypted backup and fresh-directory restore. Both source
  and target image/Compose contracts must match the reviewed pair; current
  startup still refuses legacy state. The real older-image drill preserves the
  original quorum, cluster, secret access restrictions and root revocation.
  Rollback needs the corresponding old data and reconciliation of later changes.
  The HTTP development stack retains local recovery material.

- Qualify actual vendor receivers and external systems, including ambiguous
  effects, receiver persistence and recovery. The test receiver and simulator are
  evidence of protocol behavior, not qualification of a shop or machine.
- Complete retention/storage policy, operator lifecycle, coordinated incident response,
  monitoring, off-host restores and external-secret recovery. Agent-secret
  rotation does not revoke already-issued access tokens.
- Qualify the [ACME issuer](ACME.md) against the selected public provider and DNS
  route, including service-failure alerts and certificate expiry. Issuer account
  storage has separate custody from TLS deployment archives. [Encrypted issuer
  recovery](ACME_RECOVERY.md) preserves account identity in a same-host storage-loss
  drill; off-host custody, account/image/trust migration and generation retention remain work.

Managed adapter identities now have exact-path policy checks, bounded token TTLs,
journaled issuance/rotation and policy revocation. The workload live drill covers
lost remote responses, the actual contained client, independent operator-token
use after root retirement, restored receipts and denial for already-issued
tokens. Operator enrollment, renewal scheduling and coordinated receiver
credential changes still require deployment-specific procedures.

OpenBao operator login uses Keycloak browser device authorization, a dedicated
client/audience, exact subjects and named grants. OpenBao tokens last at most five
minutes. Full administrators have OpenBao-wide authority; workload rotators can
mint only their named workload credentials and therefore can assume that
workload's secret-reading authority. Policy denial revokes existing operator
tokens' access; it cannot undo independently minted credentials or an
administrator's other policy changes. Dual configuration journals block backups
and identity rotation until recovery finishes. `test_secrets_identity_live.py`
exercises actual login, scoped denials, root retirement, interrupted recovery,
signing-key synchronization and restored access. Pinned keys require explicit
synchronization; this is not automatic JWKS refresh or emergency key revocation.

The supplied deployment supports managed RS256 signing-key rotation with a
recorded overlap and separate retirement, including cache clearing and encrypted
recovery. An explicit early-revocation command requires the recorded previous
key ID and a verified replacement, confirms the API is stopped before disabling
the key, and preserves its retry intent across lost responses. The live signing
drill verifies rejection of an unexpired old token and preservation after restore.
OpenBao trust, other clients' caches and already-admitted work still require
separate incident actions. See [credential operations](CREDENTIALS.md). It does not manage
master-realm keys or prove every offline client refreshed before retirement.

See [service workers](SERVICE_WORKERS.md), [application packaging](RUNTIME.md),
[TLS](TLS.md), [Python execution](PYTHON_API.md), [adapters](ADAPTERS.md) and
[recovery](RECOVERY.md) for exact operational limits and procedures.
