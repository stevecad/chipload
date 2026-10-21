# Threat model

This describes the implemented single-host deployment and its remaining security
work. Listeners default to host loopback; explicit client IPv4 binding is available
with supplied deployment certificates, while administration stays local. Public internet qualification
is an open milestone in [the roadmap](../ROADMAP.md). The model assumes the
operator controls the host, Docker daemon, configuration and image build process.
A compromised operator or host can read and change shop data and credentials.

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

The shop and admission databases form one durable repository. The admission store
separates request-accounting writes from shop writes; it is not a security boundary
against a compromised API, broker or executor. Service processes also do not add
a filesystem or network namespace inside the API container.

## Threats, controls and evidence

| Threat | Implemented controls | Verification |
| --- | --- | --- |
| Cross-shop access or excessive authority | Verified issuer/audience/signature and explicit shop/action grants; operation ID restrictions; public projection; current grant intersection on hosted SDK calls | `test_auth.py`, `test_transports.py`, `test_programs.py`, `test_execution_transports.py`, actual Keycloak/TLS lifecycle tests |
| Public disclosure of private claims or stale publication | Explicit projection, publication selection from the database on every request, integrity-bound immutable caches, uncached observations and permissions | `test_published.py`, `test_cache.py`, process/transport parity tests |
| Source execution obtaining host data, network or credentials | Separate non-root containers, no host mounts/network, read-only image, restricted temporary storage, memory/CPU/process/output limits | `test_worker_live.py`, `test_program_live.py` |
| Forged or expensive worker output | Broker-side decoding and model validation; JSON, collection, graph, diagnostic and evaluation budgets | `test_data_limits.py`, hostile-output live worker tests |
| Request floods or expensive data operations | Shared peer/identity/shop quotas, bounded admission state, deterministic work budgets, two supervised API service workers and container limits | `test_request_limits*.py`, `test_quotas.py`, `test_service_pool.py`, `benchmark-service` |
| Replayed mutations, lost responses or ambiguous external effects | Expected revisions/generations, durable request keys and call journals, claim fencing, no automatic code/service replay, retained uncertain operation locks, receiver reconciliation | `test_lifecycle.py`, `test_programs.py`, `test_service_pool.py`, `test_execution*.py`, adapter fault tests |
| Outbound-request abuse or credential leakage | Untrusted containers have no network; operator-owned adapter bindings; pinned HTTPS trust/receiver path; bounded network work; no redirects, proxy inheritance or automatic dispatch retries; scoped AppRole/KV access | `test_remote_adapter.py`, real OpenBao and TLS receiver denials in `test_remote_adapter_live.py`, contained executor tests |
| Spoofed client addresses or failed identity trust | Exact trusted gateway address, gateway-replaced single client header, disabled Uvicorn proxy rewriting, CA/hostname verification and bounded JWKS caching | `test_proxy.py`, `test_auth.py`, `test_tls_live.py`, distinct real network clients and private-port denials in `test_edge_live.py` |
| Recovery resetting authority, history or spent allowances | Paired database backups, encrypted authenticated archives, preserved identity state, fresh restore destinations, durable incomplete/rotation markers and operator locks; separate offline OpenBao recovery preserves quorum, cluster identity and policies | `test_recovery*.py`, `test_credentials*.py`, `test_secrets_recovery*.py` |

These tests establish behavior for their stated fixtures and failure cases. They
do not demonstrate resistance to every kernel, interpreter, dependency or
distributed denial-of-service attack. The broker validates build output within
its own container budget. Hosted SDK service calls run in separate containers;
the broker retains grant checks, admission, call journals and Docker supervision.
Those service containers still have authority over the entire mounted repository,
so their trusted implementation must enforce the caller's shop/action grants.

## Remaining deployment work

- Qualify a public edge: exposure, TLS renewal, traffic policy, sustained/adversarial
  load, tenant fairness, operational latency targets and alerting. The
  [deployment benchmark](TLS.md#measure-the-deployed-path) adds routed clients,
  TLS/JWT, business queries and simultaneous Python authoring to the component
  measurements. It counts actual correct results, refusals and missed scheduled
  requests. Its compact, single-shop fixture does not establish WAN capacity or
  independent tenant fairness.
- Reduce worker-host authority and strengthen isolation for hostile tenants.
  The broker's Docker socket remains a host-level trust dependency. Shop-specific
  dependency installation and custom evaluation environments need their own design.
- Qualify production OpenBao operator enrollment, MFA and emergency access,
  audit retention and replacement-host recovery. The separate [private secrets
  service](SECRETS.md) now supplies TLS, PGP-encrypted threshold initialization,
  explicit unsealing, audit storage and certificate reload. Its tests verify
  actual AppRole denials and sealed restart. Encrypted offline backup/restore is
  tested after deleting the original store, including quorum, cluster identity,
  revoked root-token and workload-policy preservation. This covers fresh storage
  on the same host; replacement-host and custody arrangements need qualification.
  The HTTP development stack retains local recovery material.

- Qualify actual vendor receivers and external systems, including ambiguous
  effects, receiver persistence and recovery. The test receiver and simulator are
  evidence of protocol behavior, not qualification of a shop or machine.
- Complete retention/storage policy, operator lifecycle, emergency signing-key revocation,
  monitoring, off-host restores and external-secret recovery. Agent-secret
  rotation does not revoke already-issued access tokens.

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
recovery. See [credential operations](CREDENTIALS.md). It does not manage
master-realm keys or prove every offline client refreshed before retirement.

See [service workers](SERVICE_WORKERS.md), [application packaging](RUNTIME.md),
[TLS](TLS.md), [Python execution](PYTHON_API.md), [adapters](ADAPTERS.md) and
[recovery](RECOVERY.md) for exact operational limits and procedures.
