# Software defined shops

## Goal

Build Chipload into a secure, deployable platform that lets a shop define itself in ordinary Python. The shop's own agent can author, build, inspect, test, publish, maintain, and roll back that code. People and software can discover what the shop can do through a documented REST API; agents can use MCP by default. Both interfaces use the same domain services, published revisions, and authorization rules.

A shop definition covers resources, manufacturing processes, materials, tooling, fixtures, inspection, outsourced services, dependencies, constraints, workflows, business policies, and callable operations. It must express how these things work together, including the conditions and evidence behind a capability claim.

The current stage focuses on the shop itself and all its variables. Include material preferences/exclusions, prototype and production work, recurring contracts, release schedules, inventory services, and qualified service expectations. RFQ intake and processing are deferred. Model reported practices faithfully without turning approximate limits or typical behavior into guarantees.

Steve integration is planned for a later stage.

## Current state

- The repository has issue-driven contributor tooling and a legacy v0.1.0 data specification.
- Local Compose infrastructure provides Keycloak, PostgreSQL, and OpenBao, with bootstrap and access-denial tests.
- A separate private TLS OpenBao container supports PGP-encrypted threshold initialization, explicit unsealing, audit storage, certificate reload and initialized-store checks. Operator helpers manage scoped AppRole provisioning, verification, rotation and revocation, with subject-bound Keycloak device login and explicit signing-key synchronization. Live drills exercise access denials, interrupted bootstrap/credential/identity changes and encrypted offline recovery after deleting the original store. See `docs/SECRETS.md`; operational custody, emergency access and replacement-host qualification remain open.
- The experimental Python SDK, validation, artifact codec, evaluator, contextual variable queries, and public catalog projection are implemented. A fictional shop has sixteen executable manufacturing scenarios; a heat-treatment-only example adds nine scenarios; the Swiss shop example covers shop variables and business rules. Tests also cover a document-review service with no physical resources or materials.
- The process library adds 88 process projects and one mixed shop with 2,147 technical/business cases. Every project has prototype/production acceptance conditions, contextual business variables, and portable Python source. The mixed shop models shared equipment and explicit route compatibility. Exact quantity increments support whole-item business rules without introducing shop types into the engine. See `docs/PROCESS_EXAMPLES.md`.
- A local application service now stores immutable source revisions, durable build jobs, exact publications, rollback/withdrawal, and audit events. It enforces shop/action grants. A restricted Docker worker and broker build Python projects; the broker validates models and acceptance results. Real-container tests cover hostile source and the publication lifecycle.
- REST/OpenAPI and Streamable HTTP MCP share discovery, evaluation, authoring, publication, evidence, and observation operations. The server verifies Keycloak access tokens and applies explicit shop grants; a separate broker processes queued jobs. Public results have a distinct privacy projection.
- Data boundaries now enforce JSON depth/token/number limits, model collection limits, bounded diagnostics, and deterministic evaluation/result/acceptance-suite budgets. Graph ordering avoids repeated scans. Adversarial parser, graph, transport, and container tests cover rejection and continued operation.
- Bounded caches retain validated immutable builds and public catalog bytes while database publication selection, report integrity, permissions, observations, and evaluation time remain live. Concurrent misses within a process share validation; saturation is retryable. `scripts/benchmark-data --service` measures compact/large-catalog reads with retention disabled and enabled. Cold-validation cost and deployment load qualification remain work.
- API domain calls now run in two supervised service processes with CPU, address-space, wall-time and IPC bounds. Tests cover resource exhaustion, shutdown, parent death, permission/publication changes and a committed write with a lost response. `scripts/benchmark-service` measures actual HTTP results and rejections, including the restricted API image. See `docs/SERVICE_WORKERS.md`; full deployment qualification remains separate.
- Hosted Python SDK calls run in a separate trusted service container per run, with database access and no Docker socket, settings, credentials or network. Calls respect CPU/wall budgets, cancellation and claim fencing; ambiguous calls retain started journals, and recovery removes both run containers before releasing capacity. Tests exercise actual containment, resource failures and lost committed responses. `scripts/benchmark-python` measures batching; container lifecycle cost dominates short programs and remains an optimization target. See `docs/PYTHON_API.md`.
- Durable source/build quotas now cover deployment, shop, and verified identity scopes, including pending/running jobs, hourly admissions, retained records, and payload storage reservations. Brokers claim work atomically with persistent dispatch turns. Tests exercise competing processes, transport parity, idempotent retries, migration, and operator policy changes.
- HTTP admission now shares deployment, connection-peer, identity, and mutation budgets across API processes. Limiter state is bounded; contention and saturation return retry details. Tests exercise two actual HTTP servers, process restart, forwarded-header spoofing, and MCP client recovery. `scripts/benchmark-requests` measures admission overhead. Network-edge controls and full deployment load qualification remain open.
- Capacity policy and request balances use a separate durable store, keeping public admission independent of shop write locks. Resumable migration preserves existing policy history and spent allowances; tests exercise concurrent startup, interruption, store pairing, and discovery during authoring writes. Both database files belong to one backup/restore set.
- API and broker container packaging now uses pinned images, locked wheels, non-root users, private shared storage, process limits, and graceful shutdown. The API has no Docker access; the broker has a daemon socket and no IP network. A live container test covers MCP authoring, isolated builds, REST discovery, restart, rollback, and withdrawal. See `docs/RUNTIME.md`.
- The TLS topology adds a pinned Caddy gateway, production-mode Keycloak, private application/database networks, explicit CA trust, and one-shop agent provisioning. Its separate client/admin host listeners bind to loopback. Live checks exercise real tokens, verified HTTPS, certificate/CA replacement, the agent lifecycle, restart persistence, trust failures, forwarding-header spoofing, and network/port restrictions. See `docs/TLS.md`; public routing, production secrets delivery, automatic renewal, and deployment load qualification remain open.
- A contained executor now supports offline simulators and the bounded `chipload-receiver-1` HTTPS protocol with scoped OpenBao AppRole credentials. Configuration fingerprints pin receiver routes; uncertain work queries durable receipts without repeating commands. See `docs/ADAPTERS.md`. Production OpenBao lifecycle, qualified vendor bridges, additional dependency environments, custom executable evaluation, and hardened internet deployment remain work.
- Encrypted recovery now snapshots both shop databases, Keycloak, grants, and TLS/secrets state together. Restore targets fresh state, retains spent request capacity, blocks application startup during interruption, and resumes the same recovery attempt. A live drill checks real identity/signing keys, publication and observation history, REST/MCP discovery, and interruption before backup completion and after identity restore commits. See `docs/RECOVERY.md`; external secrets and off-host disaster recovery remain operational work.
- Operator credential rotation now journals agent-secret and identity-database password changes, retries the same pending credential, verifies old-credential rejection, and preserves subjects and grants. Realm RS256 rotation records its provider/key IDs, verifies new tokens, preserves the old key during a timed overlap, and retires it through a separate command that clears the API key cache. Signing receipts and keys belong to encrypted recovery. Startup and backups reject unfinished rotations. See `docs/CREDENTIALS.md`; operator-account lifecycle, emergency key revocation, and production OpenBao delivery/rotation remain work.
- No open GitHub issues existed when this plan was written. The milestones below are a local implementation backlog; they have not been published as issues.

The existing Compose configuration is for local development. Completing a local demo will not complete the deployment goal.

## Agent interface: shop as code

MCP exposes four tools: run Python, inspect a run, cancel a run, and discover
public capabilities. `chipload.client` composes the existing service operations
from ordinary Python, both inside contained runs and over verified HTTPS.
Steve's source/build/inspect workflow informed this design.

Each SDK call retains caller and shop scope, service validation, request budgets,
and a durable call journal. Code has no network, database or equipment secrets.
Failed or interrupted programs never automatically rerun; committed calls remain
committed. REST remains independently usable. See `docs/PYTHON_API.md` for the
execution contract, deployment migration, examples and current bounds.

Additional dependency environments, stronger isolation and public deployment
qualification remain open. Keep future shop features available through Python
without multiplying MCP tools.

## Design commitments

### Work with ERP systems

Chipload is not an ERP. Order management, purchasing, inventory management, production planning, accounting, and invoicing are out of scope.

Chipload owns shop definitions, capability evaluation, business rules, and callable operation contracts. ERP systems remain authoritative for their business records. Adapters and APIs will connect the two: Chipload can use ERP observations when evaluating a shop and expose capabilities and operation results for ERP clients.

Imported observations carry their source, external record reference, and freshness. An inventory observation can inform availability without becoming an inventory ledger. A shop can describe recurring production arrangements without Chipload managing contracts or generating a production plan. Capability workflows describe process dependencies and permitted operations; ERP workflows stay in the ERP.

### A shop is a Python project

Ordinary Python modules, functions, reusable components, and pinned dependencies must be usable to define a shop. An agent needs access to the SDK reference, source files, revision history, diagnostics, and test results. The SDK must support composition across files and must not depend on an LLM to interpret a finished definition.

The engine must be abstract. Shop projects define their own resource kinds, processes, input catalogs, state properties, and business categories. Material, equipment, and physical-site requirements are optional and apply only when a shop declares them. New kinds of shops must work without shop-type branches in core evaluation. Shared vocabularies and reusable components can be developed as shop libraries.

Source revisions and build artifacts have different identities. A saved draft may be incomplete. A successful build identifies the exact source, dependencies, SDK, and runtime that produced it. Publishing selects a validated artifact; it does not silently build whatever source happens to be current.

Public discovery reads the published model. Shop-specific executable evaluation, when necessary, runs through a restricted worker with a defined input/output contract. Importing shop Python in a public API process is outside this design.

### Capabilities describe outcomes

The model must answer what a shop can make or provide, under what conditions, using which processes and resources, and with what supporting evidence. Software operations such as reading a controller or submitting a job are related to those capabilities but have their own contracts and permissions.

Capability descriptions must account for material, geometry, size, tolerances, setup, tooling, fixtures, inspection, and dependencies where relevant. A machine's advertised travel does not establish a complete manufacturable envelope. Independent process limits must not be combined into a workflow without checking compatibility between steps.

Evaluation returns `supported`, `unsupported`, `conditional`, or `unknown`, with machine-readable reasons and a readable explanation. `Supported` means supported by the specified model, evidence, and inputs; it is not a physical qualification or a reservation. Missing critical facts, unresolved geometry checks, and stale evidence must remain visible.

Declared, demonstrated, and qualified claims must remain distinguishable. Evidence needs provenance, scope, and applicable dates. Installed capability and changing observations such as availability, inventory, and calibration have separate revisions or observation timestamps. Simulated observations must stay explicitly simulated.

### REST and MCP are first class interfaces

REST must support ordinary HTTP clients without an agent or an MCP session. Publish an OpenAPI contract and examples using `curl` and a small non-agent client. MCP exposes the same application services through suitable tools and resources.

The following are required interface families; exact routes, tools, and schemas will be designed from the shop examples:

| Interface family | Required behavior |
| --- | --- |
| Discovery | Read a published shop profile; list and inspect published capabilities, processes, resources, input catalogs, variables, and workflows; filter and paginate results |
| Evaluation | Submit structured requirements and receive supported, unsupported, conditional, or unknown results, including reasons and dependencies; query the applicability of shop variables in context |
| Evidence and observations | Read authorized evidence and timestamped observations with provenance and freshness |
| Authoring | Read and edit source projects with revision guards; access the SDK reference; submit builds; inspect diagnostics and artifacts; run shop tests |
| Publication | Inspect revisions, publish an exact build, update publication, withdraw it, and roll back under explicit permissions |
| Operations | Discover authorized callable operations; validate requests; execute or cancel permitted work; inspect durable outcomes |

Anonymous discovery exposes only fields and capabilities that the shop explicitly publishes. Private information and private operations require authentication and shop-scoped authorization. A shop's own agent may receive authoring and publication permissions; human approval is a shop policy choice rather than an unconditional platform requirement.

REST and MCP must agree on domain schemas, visibility, result meanings, and permission decisions. Transport wrappers may differ. Results identify the model revision and relevant observation timestamps; REST supports conditional reads where appropriate. Errors, pagination, request limits, retry behavior, and versioning are part of the contract.

### Deployment enforces the boundaries

Separate authoring, build/evaluation workers, published discovery, and equipment adapters. Shop source is executable code and is not trusted merely because an agent wrote it. Builds need resource limits, a restricted filesystem and network, pinned dependencies, and a way to terminate and clean up jobs. Private equipment networks and credentials must not be available to discovery or build workers.

Keycloak supplies identities and role claims. Chipload enforces permissions for each shop, resource, and action, including publication and operation execution. OpenBao supplies narrowly scoped workload secrets to adapters. Neither discovery responses nor agent authoring tools return resolved equipment credentials.

Internet deployment must include tested authorization, isolation, TLS, request limits, auditability, recovery, updates, and failure behavior. Container configuration is one part of those controls. Physical safety remains enforced by equipment and site procedures; an authorization grant alone cannot bypass an equipment interlock.

## Milestones and completion criteria

Complete each milestone with runnable examples or tests where it introduces executable behavior. Keep completed work and remaining limitations visible here. The order establishes dependencies, not a promise that every domain question is already settled.

### 1. Establish the domain with a complete example shop

**Status:** Initial examples implemented. `uv run python -m examples.reference_shop` exercises sixteen manufacturing scenarios; `uv run python -m examples.heat_treatment` exercises nine heat-treatment scenarios. `uv run python -m examples.process_shops` exercises 2,147 technical/business cases across 88 process projects and one mixed shop. Transport checks cover every project's public catalog, contextual policies, and technical/business outcomes; opt-in worker checks build all exported projects. Domain tests check public filtering and the Swiss business profile; abstraction tests also cover a service without materials, machines, or sites. Continue expanding these examples as shop evidence arrives.

Create a fictional, explicitly simulated Python shop project containing multiple machines, a shared tool or fixture, material-specific process limits, inspection, an outsourced process, and at least one workflow that composes them. Use ordinary Python composition across multiple files.

Pair the project with manufacturing requests and expected explanations, including:

- A request supported by a documented process and setup.
- An incompatible material and an out-of-range dimension.
- A request requiring a different fixture or an outsourced step.
- A request whose individual steps are feasible but whose combined workflow is incompatible.
- A tolerance or geometry question for which evidence is insufficient.
- A capability dependent on missing or stale calibration or inventory observations.
- A request that distinguishes capability from immediate availability.
- A public query that must not reveal a private resource, customer detail, or equipment address.

**Complete when:** the examples specify the inputs, model facts, evidence, expected outcome, and reasons for each case. They establish domain semantics before HTTP routes or MCP tool names are fixed. SDK spelling can evolve as the model is implemented.

### 2. Build the Python SDK and versioned shop model

**Status:** Initial implementation and domain tests pass, including generic input catalogs, open resource kinds, exact absolute-temperature conversion, and contextual shop-variable applicability. See `docs/SHOP_MODEL.md` for the draft schema and migration decision. More expressive process transformations, evidence lifecycle, policy composition, and custom executable evaluation remain design and implementation work; the current model does not claim general manufacturing feasibility.

Implement typed domain models, reusable shop components, explicit units, stable references, constraints, evidence, observations, and workflow compatibility. Provide structured validation errors that identify the relevant source object or field. Define deterministic serialization and compatibility rules for generated artifacts.

Preserve the useful v0.1 invariants: permanent IDs, explicit units and timestamps, honest unknown values, freshness, and simulated provenance. Write the migration and versioning decision before treating legacy files as executable projects.

**Complete when:** the example shop builds into a validated artifact; invalid references, cycles, conflicting identifiers, incompatible units, and invalid constraints produce actionable diagnostics; the same source and pinned build environment produce the same model identity; every example query has a domain-level test.

### 3. Implement the authoring and publication lifecycle

**Status:** The local service, operator CLI, SDK schema reference, revision/publication guards, container broker, diagnostics, acceptance cases, rollback, withdrawal, and expired-job recovery are implemented and tested. REST/MCP and verified remote identities now expose the lifecycle; the queue process supervises local jobs. See `docs/AUTHORING.md` and `docs/API.md`. Additional pinned dependency environments, binary project assets, custom executable evaluation, and production supervision remain; this milestone is not yet complete.

Implement source storage and revision guards; SDK reference access; isolated, bounded build jobs; build diagnostics; model inspection; shop tests; immutable artifacts; and atomic publication of an exact revision. Include update, withdrawal, and rollback behavior. Keep publication authorization independent from source editing.

Specify how custom evaluation code is packaged, versioned, restricted, and invoked. Any executable extension must have reproducible inputs, bounded execution, and explicit handling of failure or incomplete results.

**Complete when:** an agent can create the example shop, repair a failed build using diagnostics, pass the scenario tests, and publish it with delegated permissions. Concurrent edits cannot silently overwrite each other. A failed build leaves the previous publication intact. Updating and rolling back select the intended revisions; withdrawing a publication removes public access under a documented cache policy.

### 4. Deliver REST discovery and capability evaluation

**Status:** REST routes, OpenAPI, public projections, filters/pagination, conditional reads, capability/variable queries, authorized evidence/observations, and revision metadata are implemented. Tests cover privacy, unknown values, stale observations, conflicts, malformed requests, token failures, and shop access denials. Internet deployment and load qualification remain separate gates.

Implement the shared domain services and the REST interface with OpenAPI documentation. Support published profiles, capabilities, workflows, authorized evidence and observations, and structured evaluation. Include visibility filtering, bounded pagination and filters, stable identifiers, revision metadata, and structured errors.

Anonymous visitors must be able to read public shop data using ordinary HTTP. Private queries use authenticated access. Complex evaluations may be asynchronous when needed; polling, cancellation, retention, and quotas must be explicit.

**Complete when:** `curl` and a non-agent client discover the example shop and run the scenario matrix. Public responses never contain private source, secret references, equipment addresses, or unpublished records. Tests cover pagination, revisions, malformed requests, stale observations, and access denials.

### 5. Deliver MCP and REST authoring parity

**Status:** The official MCP SDK exposes four tools and resources. Contained Python programs compose the same scoped operations as the independent REST API. Tests cover equivalent results and permissions, a real HTTP MCP client, and real Keycloak-issued tokens. The opt-in full authoring test uses an MCP client plus a separate Docker broker. OAuth clients are pre-registered; managed client onboarding and production identity operations remain deployment work.

Expose discovery, evaluation, and the authoring/publication lifecycle through both transports. Derive tool and resource contracts from the working domain model. Implement remote MCP authorization and discovery using the applicable protocol specification, with Keycloak as the identity provider.

**Complete when:** equivalent REST and MCP requests return equivalent domain results and permission decisions for anonymous, authorized, unauthorized, and cross-shop callers. A shop agent can complete the authoring loop using MCP. A conventional program can perform the same authorized lifecycle through REST. Normal discovery never requires an agent.

### 6. Make authorized shop operations callable

**Progress:** Python operation contracts, explicit simulation/live intent, operator-owned adapter bindings, operation-level grants, shared REST/MCP submission/inspection/cancellation, quotas, resource exclusion, audit events, and a separate simulated executor are implemented. Tests cover retries, changing preconditions, cancellation races, claim fencing, uncertain outcomes, reconciliation, migration, and recovery snapshots. The callable shop example covers machine, human-review, inspection, and external-service contracts with business conditions. See `docs/OPERATIONS.md`. The HTTPS receiver protocol, exact-path OpenBao delivery, executor containment, child termination, and supervised restart are implemented; see `docs/ADAPTERS.md`. Vendor bridges, physical qualification, durable orchestration between operations, external-state recovery, and the ERP integration contract remain unfinished.

The live TLS/Keycloak check authors and builds the actual example through MCP,
then exercises all four simulated calls through REST/MCP and a separate executor.
A process-crash test exits after committing an effect receipt and confirms that
reconciliation recovers the result without repeating the effect.

Define operation inputs, outputs, side effects, permissions, preconditions, and job states. Implement adapter contracts with narrowly scoped secrets, durable request identities, audit records, timeouts, cancellation semantics, and recovery from ambiguous outcomes. Retries must not silently repeat physical effects.

Provide a simulated adapter and end-to-end workflows covering equipment operations, human tasks, inspection, and external services. The first network protocol is the documented Chipload HTTPS receiver contract, exercised with an external evidence service; vendor adapters require their own integration design. A simulation passing must not be recorded as equipment qualification.

Define an ERP integration contract for reading relevant business observations and exposing shop capabilities and operation results. Preserve external record references and freshness, and test failure and retry behavior. ERP business workflows and record ownership stay in the ERP.

**Complete when:** authorized clients can submit and inspect work through REST and MCP; unauthorized and cross-shop actions fail; duplicate requests do not duplicate simulated effects; interruption and recovery tests preserve an honest operation state; adapters receive only their own credentials.

### 7. Harden and package internet deployment

Produce a documented threat model and production container topology based on the implemented interfaces. Implement and verify production identity settings, TLS, secret delivery and rotation, worker isolation, public/private network boundaries, dependency and image pinning, resource limits, logging, and recovery.

Cover authorization on every entry point, malicious source and dependencies, traversal, outbound-request abuse, credential leakage, token validation, cache visibility, and denial of service. Define and measure discovery latency, throughput, and bounded evaluation time against a reproducible workload; choose performance targets from that workload.

Bound model graph complexity and the CPU/memory spent validating worker output and evaluating published data, in addition to limiting source containers. Request-byte limits alone do not bound graph-processing cost. Exercise per-identity/shop quotas and fair queueing under adversarial workloads before internet deployment.

**Progress:** `docs/THREAT_MODEL.md` records the implemented authority boundaries,
test evidence and remaining deployment work. Structural admission and deterministic work budgets are implemented;
see `docs/SHOP_MODEL.md`. `docs/AUTHORING.md` records reproducible data-operation
measurements. Immutable build/projection caching now reduces repeated-read cost
under memory and concurrency budgets, with publication/corruption/withdrawal
tests. Source/build quotas and fair dispatch are enforced across processes using
the same repository. Shared HTTP and mutation rate controls now reject excess
traffic before shop work. Request accounting no longer competes with shop writes;
its own concurrent writers can still contend. API/broker containers now bound
memory, CPU, process count, temporary storage, and log retention. A TLS gateway
and production-mode Keycloak now provide verified identity paths, private
networks, bounded gateway requests, and exact client-address trust. All host
listeners remain on loopback. API domain processing now has per-request CPU/wall
deadlines and bounded service processes, with no automatic replay of interrupted
mutations. Reproducible HTTP measurements include container limits and rejection
rates. Hosted Python SDK dispatch now has its own contained service and requires
confirmed cleanup of both run containers. Public-edge qualification, production
secrets delivery/rotation, off-host and external-secrets recovery, broader
storage/retention, deployment load qualification and full adversarial deployment
verification remain open. The component benchmarks and HTTP admission tests
cover these implemented boundaries; production deployment remains a separate gate.

Coordinated encrypted backup/restore now preserves identity and shop state,
including authorization bindings and request balances. Interrupted backup and
restore are exercised against the actual containers; restored services remain
offline until completion. This covers the single-host TLS topology, not OpenBao,
external systems, or a qualified recovery onto a replacement host.

Agent-secret and identity-database password rotation now have durable retry,
old-credential rejection, unchanged-grant checks, and a recovery drill using the
rotated credentials. Already-issued JWTs retain their normal expiry. Managed
operator lifecycle and emergency key revocation remain separate work. Per-adapter HTTPS secret delivery is
implemented and exercises actual KV and AppRole rotation in the live test.

The separate TLS OpenBao deployment provides encrypted quorum bootstrap and
offline encrypted backup/restore. Its recovery drill deletes the original store,
restores fresh storage, and verifies the original unseal quorum, cluster ID,
workload permissions, revoked root token and audit history. Interrupted backups
block startup and resume against the original intent. This is a single-node
recovery path; replacement-host qualification, external credential reconciliation
and operational custody remain deployment work. See `docs/SECRETS.md`.

Managed workload provisioning now creates exact-path policies and bounded AppRole
credentials. Rotation persists its replacement before issuance, verifies and
installs it before retiring the old credential, and resumes lost responses.
Revocation denies the policy and removes the role, including access by existing
tokens. A live drill verifies the actual contained secret client, rotation after
bootstrap-root retirement, restored workload receipts and access denials.

Realm signing rotation now preserves an explicitly timed verification overlap,
records the exact replacement provider, and supports interrupted activation and
retirement. A live Keycloak drill checks old/new tokens, early-retirement refusal,
cache clearing, lost responses and encrypted recovery of the resulting keys and
receipt. This manages the supplied realm's RS256 keys; master-realm keys and
emergency revocation remain operator lifecycle work.

Keycloak device login now supplies subject-bound OpenBao operator grants, with
separate client/audience claims and explicit administrator or named-workload
rotation authority. Configuration and revocation retain matching intent journals
in both deployments; completed unaffected grants can authenticate during recovery.
Pinned signing keys are synchronized explicitly after activation and retirement.
The identity live drill covers browser approval, root retirement, scoped denials,
interrupted configuration/revocation, key rollover and login after fresh OpenBao
storage recovery. User enrollment, MFA policy, emergency access, automatic root
retirement and coordinated replacement-host recovery still need qualification.

**Complete when:** a clean installation can be configured from documented inputs; automated security and integration checks pass; backup/restore and credential rotation are exercised; failed identity/secrets dependencies fail safely; deployment tests verify exposed ports and network access; load-test results and operational requirements are documented. Local development settings must not pass production validation.

### 8. Demonstrate the whole workflow and prepare contributions

Run the full lifecycle from a fresh checkout and a newly authored shop project. Demonstrate discovery by an independent agent and by a conventional REST client. Demonstrate updates, rollback, isolation between shops, restricted operations, and recovery.

Update `SPEC.md`, `AGENTS.md`, README, SDK/API reference, deployment instructions, and issue briefs to match the working implementation. Keep the contributor issue workflow authenticated through the contributor's own `gh` login.

**Complete when:** the documented end-to-end checks run successfully, the interfaces agree, deployment requirements are met, and remaining unsupported integrations are explicit. No milestone is complete solely because its design or a stub exists.

## Working rules

- Work from representative shop behavior outward to SDK, services, transports, and deployment.
- Keep the Python source authoritative; generated public models are build artifacts.
- Require evidence for claims about physical capabilities and production readiness.
- Keep REST clients and agent clients equally supported.
- Record material design decisions and unresolved questions as implementation proceeds.
- Do not publish GitHub issues, send messages, or deploy publicly merely because this local roadmap exists; those actions follow the user's authorized task.
