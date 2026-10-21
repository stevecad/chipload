# Private OpenBao service

`scripts/secrets-stack` runs a separate TLS OpenBao container on an existing
internal adapter network. It uses pinned OpenBao 2.7.1, persistent Raft storage,
file audit logging, PGP-encrypted initialization material, and explicit threshold
unsealing. It publishes no host ports and mounts no shop databases, application
settings, Docker socket, or PGP private keys.

This is a single-node deployment building block. It does not yet provide Raft
backup/restore automation, replacement-host recovery, automatic certificate
delivery, workload-identity rotation, root-token retirement, or high availability.
The [shop/Keycloak recovery archive](RECOVERY.md) does not contain this separate
service. Complete its recovery and credential lifecycle before production use.

## Configure and start

Use Linux, a local Docker Engine/Compose, Python 3.12+, `uv`, and GnuPG. Run as
the non-root deployment owner. Supply a certificate chain and matching key named
`server.crt` and `server.key`, and the CA bundle that verifies the chain. The
certificate must cover the hostname used by adapters on their private network.
The helper verifies the actual chain, hostname, and served leaf certificate.
It does not generate or install a development CA.

Each unseal custodian supplies their encryption-capable public PGP key, in binary
or ASCII-armored format. Supply a public key for encrypting the initial root token
as well; it can belong to one of the custodians. No private PGP key belongs on
the service host. The helper rejects private-key inputs and duplicate share
recipients. Choose 2–10 recipients and a threshold of at least two. Distinct
public keys alone do not establish independent human custody.

```sh
uv sync --locked --extra server
docker network create --internal chipload-evidence
uv run --extra server python scripts/secrets-stack --state .local/secrets configure \
  --name chipload-secrets --network chipload-evidence --host bao \
  --certificates /secure/bao-tls --ca-bundle /secure/bao-ca.pem \
  --pgp-key /secure/alice-public.pgp --pgp-key /secure/bob-public.pgp \
  --pgp-key /secure/carol-public.pgp --root-pgp-key /secure/operator-public.pgp \
  --threshold 2
uv run --extra server python scripts/secrets-stack --state .local/secrets up
uv run --extra server python scripts/secrets-stack --state .local/secrets initialize
```

The state parent and state directories must be owner-only. `configure` refuses
existing state and a reused Compose project. `up` starts the service and reports
its initialized/sealed state; a running TLS listener does not mean secrets are
available. Container health is unsuccessful while sealed or uninitialized.
No command automatically unseals the server. Use the same `--state` for every
operation on this deployment.

Initialization sends public PGP keys to OpenBao's
[`sys/init` endpoint](https://openbao.org/docs/api/system/init/). OpenBao encrypts
the unseal shares and initial root token before returning them. The helper saves
only encrypted material in private, fsynced `bootstrap.json`; it never prints
the response or passes shares/tokens through command-line arguments. The file
records recipient fingerprints, ordered `keys_base64`, the encrypted `root_token`,
and the threshold. Preserve it off-host with the deployment's recovery records.

Before initialization, `.initialize-incomplete.json` records the intended
deployment. If execution stops after `bootstrap.json` is durable, rerun
`initialize` to finish the receipt, even with the container stopped. It does not
send another initialization request. If no encrypted response was saved, the
tool refuses to replay or replace initialization. OpenBao returns that material
once; an initialized server whose response was lost may be unrecoverable without
independently retained material. Preserve the data and investigate before
abandoning a fresh instance. The tool has no reset/delete-storage command.

## Unseal and provision workloads

Deliver each encrypted share only to its intended custodian. Base64-decode that
custodian's `keys_base64` entry to a PGP message, then decrypt it on their protected
workstation. For example, with a private working directory and restrictive umask:

```sh
umask 077
gpg --output /secure/unseal-share --decrypt /secure/unseal-share.pgp
uv run --extra server python scripts/secrets-stack --state .local/secrets unseal \
  --share-file /secure/unseal-share
```

The share file and its parent must be owner-only; keep decrypted shares outside
service state. Submit the threshold number of distinct shares. A repeated share
does not add another custodian. The helper reads one share for that request and
reports only seal/progress information. Remove the temporary plaintext according
to your custody procedure; filesystem deletion is not secure erasure. Operators
must protect memory, terminals, swap, backups, and their transfer channel too.

After the first successful unseal, `initialized.json` pins the logical OpenBao
cluster ID. Later unsealing against a different cluster stops the owned container
and refuses the operation. Missing initialized storage also prevents startup;
the helper never silently creates an empty replacement. These are recovery/error
checks, not protection against an administrator controlling the Docker host.

Use `status` to obtain the current bridge IP, TLS server name, CA path and seal
state. The Linux operator host can reach that private bridge address. Configure
the official OpenBao CLI's `BAO_ADDR`, `BAO_TLS_SERVER_NAME` and `BAO_CACERT` from
those values, without disabling verification. Adapters use `https://bao:8200`
through Docker DNS. Administration remains a privileged operator workflow.

Decrypt the initial root token only on a protected operator system. Provision
managed operator access, a KV v2 mount and one narrowly scoped AppRole per adapter
as described in [ADAPTERS.md](ADAPTERS.md#configure-a-receiver-and-its-secret).
Verify the workload's permitted read and denied cross-shop reads, writes, lists
and administration. Then revoke the bootstrap root token through OpenBao
administration. Do not retain it in an executor credential directory or grant
operator access to shop code. Automated provisioning/rotation and root-token
retirement are further work; successful unsealing alone does not complete setup.

## Storage, TLS and audit operation

```sh
uv run --extra server python scripts/secrets-stack --state .local/secrets status
uv run --extra server python scripts/secrets-stack --state .local/secrets down
uv run --extra server python scripts/secrets-stack --state .local/secrets up
```

`down` preserves `data/`, `audit/` and bootstrap material. A restart seals the
server and requires the threshold again. Sealed/unavailable OpenBao prevents
the adapter from sending a new receiver command; an earlier uncertain operation
remains uncertain. Never copy a live Raft directory as if it were a consistent
backup. Use a verified OpenBao recovery procedure and preserve unseal custody;
this helper's restart test does not demonstrate disaster recovery.

For renewed certificates, replace `certificates/server.crt`, `server.key` and
`ca.pem` while preserving mode 0600, then run `secrets-stack reload`. It sends
SIGHUP and verifies that the served leaf now matches the configured certificate,
including when the CA is unchanged. Distribute new trust to workloads before a
CA cutover. The command does not generate certificates or schedule renewal.

`audit/audit.jsonl` uses OpenBao's hashed audit fields with `log_raw=false` and
mode 0600. It is separate from rotating container diagnostic logs. Arrange disk
monitoring, retention and off-host collection. To rotate the audit file, rename
it on the host and run `reload`; OpenBao's
[file audit device](https://openbao.org/docs/audit/file/) reopens it on SIGHUP.
Keep auditing enabled: if the audit sink fails, secret operations can fail too.
Audit files are sensitive even when credential fields are hashed.

The container runs as the owner's non-root UID/GID with a read-only root, all
capabilities dropped, no privilege escalation, 1 CPU, 512 MiB memory with no
additional swap, 64 processes, 1024 file descriptors, disabled core dumps and a
16 MiB temporary filesystem. Memory locking is disabled for the container/Raft
configuration; secure the host's memory and swap separately. TLS requires at
least 1.2; request bodies, read/write time and request duration are bounded.
Only the selected receiver and executors should share the internal network.
The launcher rejects API/broker/source-worker membership; Docker administrators
must maintain that separation after startup. This topology supplies neither
arbitrary external egress nor multi-host high availability.

## Verify

```sh
./scripts/build-runtime
CHIPLOAD_SECRETS_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_secrets_stack_live.py' -v
```

The live test creates temporary TLS/PGP material and a fresh private network. It
checks actual container limits and mounts, encrypted bootstrap/decryption,
interrupted local completion, duplicate-share rejection, distinct-share unseal,
scoped AppRole permissions, audit redaction and the real Chipload secret-fetch
client in a restricted container. It replaces CA/leaf certificates, rejects old
trust, verifies sealed restart and secret persistence, and refuses missing or
mismatched initialized storage. Test-owned containers, network and temporary
files are removed. Custody, production certificates, off-host recovery and
operational monitoring need deployment-specific verification.
