# Chipload

Chipload is becoming a platform for software-defined shops. A shop defines its resources, processes, capabilities, constraints, and workflows in Python. Its own agent can help build and maintain that code, and other software can discover what the shop can do.

[Read the roadmap](ROADMAP.md) for the goal, design commitments, and implementation milestones. The [v0.1.0 specification](SPEC.md) records the original data model for shops, sites, resources, capabilities, and timestamped status reports. Its file-format scope describes that release; the roadmap defines the broader product direction.

The shop authoring SDK and initial runtime use Python. A documented REST API lets websites, scripts, and other clients query shop data without an agent. MCP exposes the same domain services and permission rules for agents. Public discovery exposes the capabilities a shop explicitly publishes.

The API and build broker can run in separate containers with locked dependencies, private persistent storage, and process limits. See [application container setup](docs/RUNTIME.md) or the [TLS gateway and Keycloak stack](docs/TLS.md) for verified HTTPS, private networks, and a shop agent identity. Host listeners remain bound to loopback; public deployment and operational qualification remain work.

The TLS deployment has [encrypted backup and restore tooling](docs/RECOVERY.md) for shop data, identity data, grants, and TLS state. Recovery preserves request allowances and keeps a restored service offline until startup.

The [credential rotation helper](docs/CREDENTIALS.md) changes the provisioned agent secret and Keycloak database password with interrupted-cutover recovery and verification that the previous credentials no longer authenticate.

Chipload is not an ERP. Order management, purchasing, inventory management, production planning, accounting, and invoicing are out of scope. Chipload will work with ERP systems through adapters and APIs. Shop capabilities, business rules, and callable operations belong in Chipload; ERP systems remain the source of truth for their business records.

The repository contains an experimental Python shop model and evaluator, REST/MCP discovery and authoring, Keycloak token verification with explicit shop grants, a separate container build broker, executable examples, and local identity/secrets infrastructure. Production deployment and privileged equipment operations remain to be implemented.

## Define and inspect a shop

The current focus is the shop itself: capabilities, resources, constraints, evidence, business policies, and other shop variables. The engine has no built-in shop types; each Python project defines its processes and vocabularies. RFQ intake is deferred.

With Python 3.12+ and [uv](https://docs.astral.sh/uv/) installed:

```sh
uv sync --locked
uv run python -m examples.reference_shop
uv run python -m examples.reference_shop --public
uv run python -m examples.heat_treatment
uv run python -m examples.process_shops
```

The reference project runs sixteen manufacturing scenarios; `--public` prints its public catalog. The [heat-treatment-only example](examples/heat_treatment.py) runs nine scenarios through the same engine, including batch limits, temperature conversion, missing process approval, and workflow order. Read [the shop model documentation](docs/SHOP_MODEL.md) for semantics, contextual variable queries, limitations, and the [Swiss shop](examples/swiss_profile.py) example. These example commands execute trusted source in your local Python process.

The [process and business example library](docs/PROCESS_EXAMPLES.md) adds 88 process examples and one mixed shop, with 2,147 executable cases. It covers machining, EDM, cutting, forming, joining, additive manufacturing, casting, molding, composites, treatment, finishing, and inspection. Every shop has separate prototype/production terms, quantity limits, input/documentation policies, delivery conditions, and contextual business variables. The mixed shop combines turning, milling, drilling, reaming, Swiss, and inspection while sharing equipment explicitly. Each example can be exported as a self-contained Python source project for the isolated worker.

To save source revisions, build in restricted Docker containers, inspect diagnostics, and publish or roll back an exact build, follow [the authoring walkthrough](docs/AUTHORING.md). See [the API guide](docs/API.md) to run REST and MCP, configure Keycloak/shop permissions, and use ordinary HTTP clients.

## Local infrastructure

With Docker Engine and Docker Compose installed, start the development stack:

```sh
./scripts/dev-stack up
./scripts/dev-stack status
```

The first command generates random credentials in an ignored `.env`, starts the containers, imports a `chipload` Keycloak realm, and initializes and unseals persistent OpenBao storage. Images are pinned by version and digest. Docker downloads the images on first use; allow a few minutes for Keycloak's initial database migration.

| Service | Local address | Purpose |
| --- | --- | --- |
| Keycloak | `http://localhost:8080` | Human/service identities and role claims |
| PostgreSQL | Compose network only | Keycloak's persistent database |
| OpenBao | `http://localhost:8200` | Equipment credentials, workload authentication, and secret-access audit logs |

Host ports bind to `127.0.0.1`. Set `KEYCLOAK_PORT` and `OPENBAO_PORT` in `.env` before starting if those ports are occupied. The admin username/password are in `.env`; the script never prints them. `.env.example` documents the settings. Docker access and the host account that owns this checkout remain trusted.

Keycloak imports a resource-server client named `chipload`, with independent `shop:read`, `shop:operate`, and `shop:admin` roles. No shop user or default role grants are created. Assign roles explicitly in the admin console. The `chipload-cli` client is prepared for authorization code with PKCE S256, an explicit `chipload` token audience, and a loopback callback at port 8765. There is no CLI login implementation yet. Password grants are disabled for these clients. Realm imports only create missing realms; editing the JSON does not update an existing realm.

Chipload validates token signatures, issuer, audience, token type, and expiry, then intersects Keycloak roles with operator-owned shop/action grants. MCP exposes protected-resource metadata and uses pre-registered OAuth clients. Access-token audience must include the exact configured MCP resource URL, initially `http://localhost:8088/mcp`; the realm template adds this mapper to `chipload-cli`. Existing realm imports are not updated automatically. See [the API guide](docs/API.md) for bindings, audience setup, and current authorization limits.

OpenBao uses a KV v2 mount at `chipload/` and a restricted `chipload-example` AppRole. Its tokens can only read `chipload/data/shops/example/*`; they cannot write secrets or administer OpenBao. No equipment credentials are seeded. Populate paths like `shops/example/mill-01` through OpenBao administration. Shop code will reference these paths; the runtime will resolve values without exposing them in MCP responses.

The bootstrap writes owner-only files under ignored `.local/dev-stack/`:

- `openbao-init.json`: administrative root token and unseal material. Keep this with the local operator; never mount it into the runtime.
- `runtime-approle.json`: restricted example-shop workload credentials. Secret IDs last 24 hours; tokens last 15 minutes, renewable up to one hour. Each `up` refreshes the Secret ID and revokes the previous one. Give the future runtime only its workload credentials.

OpenBao data, audit logs, and Keycloak data use named volumes. Run `./scripts/dev-stack down` to stop the stack without deleting them. Run `up` again to resume and unseal it. Preserve `.env` and `.local/dev-stack/` alongside the volumes: losing those files can require credential recovery. The script refuses to overwrite initialized OpenBao storage when recovery material is missing. Changing `.env` passwords after initialization does not rotate existing database/admin credentials.

OpenBao's audit device is declared in `deploy/openbao/config.hcl`. After editing that file, run `docker compose restart openbao` and `./scripts/dev-stack up` to reload it and unseal the server.

This is a local development stack. It uses HTTP, Keycloak development mode, a single OpenBao node, disabled memory locking, and one locally stored unseal share. Production deployment needs TLS with stable issuers, production Keycloak configuration, separately secured recovery/auto-unseal, protected backups, and independent workload identities for each shop. This Compose file does not claim production readiness.

The setup follows [Keycloak's container guidance](https://www.keycloak.org/server/containers), [OpenBao's persistent Raft storage](https://openbao.org/docs/configuration/storage/raft/) and [AppRole authentication](https://openbao.org/docs/auth/approle/).

## Agent development setup

Install Git, Python 3.9 or newer, and [GitHub CLI](https://cli.github.com/). Log in using your own GitHub account before asking an agent to work:

```sh
gh auth login
gh auth status
git clone https://github.com/stevecad/chipload.git
cd chipload
```

Read [AGENTS.md](AGENTS.md) for repository rules and validation. From an agent or your terminal:

```sh
# Check open issues, oldest first (default limit: 100).
./scripts/agent-work list
./scripts/agent-work list --json

# Read an issue and its comments without changing the working tree.
./scripts/agent-work brief 1

# Create agent/issue-1 from origin's latest default branch and print a task brief.
./scripts/agent-work start 1

# Alternatively, select the oldest unassigned open issue.
./scripts/agent-work start next
```

Give your coding agent this instruction to begin:

> Read AGENTS.md, ROADMAP.md, and SPEC.md. Run `./scripts/agent-work list`, review a suitable issue and existing PRs, then run `./scripts/agent-work start NUMBER`. Implement the issue using the printed task brief, run the required checks, and report the result.

The script works with any agent that can run terminal commands. It prepares work; your agent does the implementation. It requires a valid `gh` login and access to the repository configured as `origin`. Listing and previewing are read-only. Starting requires a clean working tree, fetches the default branch, and switches to a new local issue branch. Existing branches are preserved; use `git switch agent/issue-NUMBER` and `brief NUMBER` to resume.

`next` does not check dependencies or claim an issue. Review assignments and open PRs before implementing, and use a separate clone or worktree for each concurrent agent. No eligible issue is a successful no-op. The script does not launch agents, assign issues, push code, open PRs, or close issues. Publish only as authorized by your development task; a PR that resolves an issue should include `Closes #NUMBER`.

## Check the tooling

```sh
uv run --extra server python -m unittest discover -s tests -v
uv run --extra server ruff check src examples tests
git diff --check
```

The default tests check shop models, capability evaluation, artifacts, and public projections, as well as tooling with temporary files and Git repositories using a fake GitHub CLI. They do not need GitHub authentication or running containers. To check discovery, realm configuration, and actual OpenBao access denials against the local stack:

```sh
CHIPLOAD_LIVE_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_dev_stack_live.py' -v
```

Live tests create and remove randomly named test secrets. They do not contact equipment or change GitHub issues.

## License

Chipload is licensed under the [MIT License](LICENSE).
