# HTTPS adapters and contained executors

The executor supports offline simulators and one configured HTTPS receiver per
container. The receiver interface, `chipload-receiver-1`, is a Chipload protocol
for services that can persist request identities and outcomes. It is not a
machine-control standard. A device, human-task system, or ERP needs its own
qualified bridge implementing this contract. The included receiver is a test
fixture, not a production server.

[service_shop.py](../examples/service_shop.py) defines a service with no equipment
or material catalog: read an external evidence record and request its review.
Both operations enforce a prototype-use business rule. The live test calls an
actual TLS service and persists its review receipt; it does not establish
physical qualification or approval by a person. ERP orders, inventory ledgers,
purchasing, schedules, and financial records remain in the ERP.

## Trust and deployment boundary

Shop Python declares operation inputs, outputs, prerequisites, logical commands,
and effects. Only operator configuration chooses receiver URLs, OpenBao paths,
credentials, and permitted shop/operation/command/effect combinations. REST/MCP
authors cannot install adapter implementations or redirect their credentials.
Each binding pins the published model and the adapter configuration digest.
Changing a receiver, secret path, allowed contract, or timeout requires a new
binding. Uncertain calls retain their original binding and require that original
configuration for reconciliation.

`scripts/executor` starts a separate, non-root container with a read-only root,
all capabilities dropped, no new privileges, no published listener, and no
Docker socket. It shares the paired shop databases with the API. It has 1 CPU,
512 MiB memory without extra swap, 32 processes, a 16 MiB temporary filesystem,
disabled core dumps, and bounded logs. This is trusted infrastructure with write
access to the shop repository; it is not a sandbox for arbitrary adapter code.

Simulators use `network_mode: none` and mount no credentials. HTTPS executors
join an operator-created **internal local bridge**, separate from the API,
broker, and source workers. Only the selected receiver and HTTPS OpenBao service
should share it. The launcher checks existing members; the Docker administrator
must preserve that separation afterward. An internal bridge does not grant
arbitrary internet or equipment-network egress. Reaching an outside system needs
a separately secured bridge service and its integration design. The Docker host
remains an authority over containers and network membership.

Every network invocation runs in a fresh child process. The wall-time bound
includes DNS, secret retrieval, and response reads; it is 1–30 seconds, capped
by the dispatch deadline on the initial request. The child has a 256 MiB address
space limit, 10 CPU seconds, a 128 KiB input limit, a 96 KiB HTTP response limit,
and a 64 KiB result limit. Shutdown or timeout terminates the child process group.
TLS verifies the certificate chain and hostname, requires TLS 1.2 or later, and
does not follow redirects, use environment proxies, decompress responses, or
automatically retry commands. The receiver is trusted with operation inputs and
must protect them and its credential.

## Configure a receiver and its secret

Save operator configuration in a private 0600 file, for example
`.local/evidence-adapter.json`:

```json
{
  "protocol": "chipload-receiver-1",
  "id": "evidence",
  "receiver_id": "evidence-service",
  "origin": "https://receiver:8443",
  "ca_file": "receiver-ca.pem",
  "contracts": [
    {"shop_id": "service-example", "operation_id": "read-evidence-status", "command": "read-status", "effect": "read"},
    {"shop_id": "service-example", "operation_id": "request-evidence-review", "command": "request-review", "effect": "write"}
  ],
  "secrets": {
    "origin": "https://bao:8200",
    "ca_file": "bao-ca.pem",
    "auth_mount": "approle",
    "kv_mount": "chipload",
    "secret_path": "shops/service-example/evidence",
    "field": "token",
    "policy": "evidence-adapter"
  },
  "timeout_seconds": 10
}
```

Origins must be HTTPS origins without paths, queries, fragments, or user
credentials. CA names refer to files inside this adapter's credential directory;
omitting a CA name uses the image's system trust store. Do not mount CA private
keys. Configure a production HTTPS OpenBao service separately; the HTTP
development Compose profile cannot serve this adapter.

Provision a distinct [AppRole](https://openbao.org/docs/auth/approle/) with one
policy, no default or identity policies, and a token TTL/max TTL of at most 300
seconds. For this example its policy is:

```hcl
path "chipload/data/shops/service-example/evidence" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
```

Store the receiver bearer credential in the `token` field of the
[KV v2](https://openbao.org/docs/secrets/kv/kv-v2/) entry
`chipload/shops/service-example/evidence`. Provision the role ID and SecretID
through the OpenBao operator workflow. The executor's private 0700 credential
directory must contain only `role-id`, `secret-id`, and the configured CA files,
each a private 0600 regular file owned by the deployment user. Never put root or
administration tokens there. The launcher rejects extra files and symlinks.

The child obtains a fresh AppRole token per call, checks the returned policy
names and TTL, reads exactly the configured KV path, and attempts `revoke-self`
before contacting the receiver. An interrupted revocation expires with the
short token TTL. It does not cache or persist resolved secrets. OpenBao policy
contents are operator authority: matching a policy name does not prove that its
ACL is narrow, so verify denied reads, writes, listing, and administration when
provisioning it. Sealed/unavailable OpenBao prevents a new command from being
sent; uncertainty from an earlier dispatch remains uncertain.

Compute the nonsecret configuration fingerprint:

```sh
uv run python -m chipload.remote_adapter --fingerprint .local/evidence-adapter.json
```

Use that value as `adapter_sha256` in each operator-owned `AdapterBinding`, with
`adapter_id: evidence`, `mode: live`, the exact published `model_sha256`, and the
matching shop/operation. Apply it with the local `bind-operation` command from
[OPERATIONS.md](OPERATIONS.md#configure-authority-separately). Simulated bindings
omit `adapter_sha256`. No URL or secret is stored in a binding.

## Start and maintain the executor

Build matching worker, API/broker/executor, and TLS images before starting the
application. Initialize the paired databases through that application first.
Use its explicit subject/shop grants and its container settings path:

```sh
./scripts/build-worker
./scripts/build-runtime
./scripts/build-tls
# Start the application as described in TLS.md, then provision the receiver and
# OpenBao on this dedicated network before starting its executor.
docker network create --internal chipload-evidence
uv run python scripts/executor up \
  --settings .local/tls/runtime.json --data .local/tls/data \
  --adapter .local/evidence-adapter.json --credentials .local/evidence-credentials \
  --network chipload-evidence --name chipload-evidence-executor
```

Replace the TLS state path with the directory used for your deployment. Settings
must name `/data/chipload.db`; both paired database files must already exist.
Use the same arguments with `status`, `logs`, or `down`. A Compose project name
cannot be reused for a different executor configuration. `up` recreates the
container and validates image fingerprints; inspect `status` and `logs` for
runtime health. Restart policy is `unless-stopped` and shutdown grace is 10 seconds.

For a contained simulator, use `--simulators .local/simulators.json` in place of
`--adapter`, `--credentials`, and `--network`. Its adapter configuration is the
same as the host simulator in [OPERATIONS.md](OPERATIONS.md#run-the-simulator).

Changing grants or an individually mounted settings/configuration file requires
recreating the executor as well as updating the API. A credential directory is
mounted as a directory: atomic replacement of `secret-id` or a CA file is visible
on the next call without container recreation. KV credential rotation is also
read on the next call. Credentials and CA bytes are excluded from the routing
fingerprint; their file names remain pinned. Coordinate receiver and OpenBao
rotation so both accept the same bearer credential. AppRole SecretIDs expire
and must be renewed by the operator; there is no renewal daemon.

For TLS deployment settings, startup/shutdown takes the deployment operation
lock, and startup refuses incomplete backup, restore, or credential rotation.
The executor is a separate Compose project: stop it explicitly before a
[backup](RECOVERY.md). The backup does not include this independent adapter
configuration, AppRole files, OpenBao state, or receiver ledger. Protect and
recover those together according to the integration's recovery procedure.

## Receiver protocol and honest outcomes

All three endpoints authenticate the configured bearer credential. The receiver
must additionally bind that credential to permitted shop/operation/command/effect
contracts, validate the body and size, and retain its durable receipt ledger.
It must never treat client-supplied `actor` as independent authentication.

| Request | Meaning |
| --- | --- |
| `PUT /v1/executions/{execution_id}` | Accept one intent; durably deduplicate by ID and intent digest before any effect |
| `GET /v1/executions/{execution_id}` | Query the existing outcome; never re-execute |
| `POST /v1/executions/{execution_id}/cancel` | Request cancellation or fence an intent that has not arrived |

The PUT JSON contains `protocol`, `receiver_id`, `execution_id`, `intent_sha256`,
`shop_id`, `operation_id`, `actor`, `command`, `effect`, `mode: live`, an aware
UTC `deadline`, and typed `inputs` facts. Execution IDs and intent digests are 64-character
SHA-256 hex values. The cancel body contains `receiver_id` and `intent_sha256`.
The exact schemas are `ReceiverRequest`, `ReceiverResponse`, and `AdapterResult`
in [remote_adapter.py](../src/chipload/remote_adapter.py) and
[execution_model.py](../src/chipload/execution_model.py).

A 200, 201, or 202 response must contain matching protocol, receiver ID,
execution ID, intent digest, `mode: live`, and a `result` with `state`, `code`,
and `outputs`. Only `succeeded` can have outputs, which must match the published
operation's contract. `rejected` or `cancelled` asserts that no effect occurred
and no late effect can occur; persist a tombstone before returning either state.
An already accepted effect cannot be reported as cancelled. Reject conflicting
intent digests for an existing ID. An expired initial request must not begin work.

Timeouts, process loss, invalid replies, partial effects, and even authenticated
404 responses remain `indeterminate`. Reconciliation performs GET or cancellation,
never another PUT. Unknown outcomes retain resource locks and running capacity.
Polling backs off from 2 to 30 seconds per uncertain job; a new cancellation
request bypasses the earlier polling delay. Child termination does not undo an
external effect. Receivers controlling physical systems need their own durable
acceptance, deadline, stop, and recovery guarantees; this HTTP exchange alone
cannot provide exactly-once physical effects.

## Verification and remaining work

```sh
uv run --extra server python -m unittest discover -s tests -p 'test_remote_adapter.py' -v
uv run --extra server python -m unittest discover -s tests -p 'test_executor_launcher.py' -v
CHIPLOAD_ADAPTER_TESTS=1 uv run --extra server python -m unittest discover \
  -s tests -p 'test_remote_adapter_live.py' -v
CHIPLOAD_ADAPTER_TESTS=1 uv run --extra server python -m unittest discover \
  -s tests -p 'test_executor_live.py' -v
```

The Linux/Docker live test builds the actual Python service project through MCP,
uses real Keycloak tokens and TLS OpenBao ACL denials, executes a TLS receiver
through the contained executor, rotates its credential, and reconciles a lost
response across process restarts. It also tests sealed secrets, interrupted calls,
cancellation, private mounts, and network separation. Unit tests cover invalid
TLS, broad token policies, malformed/oversized replies, redirects, ignored proxy
environment, configuration changes, and recovery startup locks.

Production OpenBao provisioning, unseal/recovery, high availability, audit
retention, certificate renewal, deployment load qualification, vendor bridges,
and coordinated external-system recovery remain integration and deployment work.
