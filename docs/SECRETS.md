# Private OpenBao service

`scripts/secrets-stack` runs a separate TLS OpenBao container on an existing
internal adapter network. It uses pinned OpenBao 2.7.1, persistent Raft storage,
file audit logging, PGP-encrypted initialization material, and explicit threshold
unsealing. It publishes no host ports and mounts no shop databases, application
settings, Docker socket, or PGP private keys.

This single-node deployment includes encrypted offline backup and fresh-directory
restore. Replacement-host qualification, automatic certificate delivery,
operator authentication, automatic root-token retirement, and high availability remain
deployment work. The [shop/Keycloak recovery archive](RECOVERY.md) does not contain
this separate service.

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
operator access to shop code. The workload helper below provisions and rotates
adapter identities. Operator authentication and root-token retirement still need
an independent administrative procedure. OpenBao
[revokes descendant tokens with their parent](https://openbao.org/docs/internals/token/);
verify that the replacement operator login survives root retirement.

## Managed adapter credentials

`scripts/secrets-workload` manages one AppRole and policy per adapter, with the
policy name from its operator-owned [adapter configuration](ADAPTERS.md#configure-a-receiver-and-its-secret).
It creates missing AppRole/KV v2 mounts, an exact-path read policy, and the initial
receiver secret. Existing policies, roles or secret paths are not adopted or
overwritten. Supply a fresh workload name and secret path when provisioning.
The helper verifies the service's pinned cluster identity and TLS trust before
using an operator token read from a private file outside service/credential state.

Create the adapter's private credential directory and copy only its public CA
bundles there. The OpenBao CA must match the managed service; its configured
origin must be `https://HOST:8200`. Keep the directory and its private parent on
the same filesystem for atomic credential replacement. In this example the
adapter config uses `receiver-ca.pem`, `bao-ca.pem` and policy `evidence-adapter`:

```sh
install -d -m 700 /secure/evidence-credentials
install -m 600 /secure/receiver-ca.pem /secure/evidence-credentials/receiver-ca.pem
install -m 600 .local/secrets/certificates/ca.pem /secure/evidence-credentials/bao-ca.pem
uv run --extra server python scripts/secrets-workload \
  --state .local/secrets --operator-token /secure/operator-token provision \
  --adapter .local/evidence-adapter.json --credentials /secure/evidence-credentials \
  --receiver-secret /secure/evidence-bearer --secret-id-ttl 86400
```

The receiver secret file contains its current bearer credential; provisioning
does not change the receiver. The operator token and input secret file must be
0600 inside owner-only directories. Commands print receipts, expiry and status,
never credentials. Executors receive only `role-id`, `secret-id` and their CA
files. Neither shop Python nor MCP receives secret-management authority.

Workload tokens have one policy, no default policy, and a hard 60-second maximum
lifetime. The helper reads the configured secret, checks capabilities for the
permitted path and forbidden operations, exercises actual read/list/admin denials,
and revokes its verification token. Policy or role drift stops verification and
rotation. SecretIDs default to one day, with a configurable lifetime from five
minutes to seven days. Arrange renewal before the reported expiry; there is no
renewal daemon or automatic operator login.

```sh
uv run --extra server python scripts/secrets-workload \
  --state .local/secrets --operator-token /secure/operator-token verify --workload evidence-adapter
uv run --extra server python scripts/secrets-workload \
  --state .local/secrets --operator-token /secure/operator-token rotate --workload evidence-adapter
```

Rotation persists a random replacement before calling OpenBao's
[custom SecretID endpoint](https://openbao.org/docs/api/auth/approle/).
A retry looks up that same value and operation ID; a lost response does not issue
another credential. It verifies the replacement, atomically installs it, destroys
the previous SecretID and verifies that the previous login fails. Running
executors read the new file on the next call. Already-issued workload tokens
retain their scope until their normal expiry or policy revocation.

If a command fails, keep `.workload-incomplete.json` and run:

```sh
uv run --extra server python scripts/secrets-workload \
  --state .local/secrets --operator-token /secure/operator-token resume
```

The journal contains sensitive pending credentials and stays private outside the
container's mounts. Backup refuses unfinished workload changes. The service can
restart, be unsealed and resume the same operation. An expired/removed pending
credential is not recreated after confirmed issuance; revoke that workload and
provision a new identity. A host/process failure can leave private
`.chipload-workload-*` files beside the credential directory; clean them only
after confirming that no credential operation is running.

To retire an identity, settle operations first when possible, then run:

```sh
uv run --extra server python scripts/secrets-workload \
  --state .local/secrets --operator-token /secure/operator-token revoke --workload evidence-adapter
```

Revocation replaces the managed policy with a deny policy and deletes the AppRole.
This blocks new logins and removes that policy's access from already-issued
tokens. It can supersede an interrupted change for the same workload, even when
local credential files are missing or its policy was broadened. Retired policy
names remain reserved; they are not reused. Local credential files remain private
but no longer authenticate. Revocation cannot undo a receiver action or resolve
an uncertain operation.

Nonsecret receipts under `workloads/` record generation, cluster, adapter config,
credential directory, SecretID hash/accessor and expiry. They are included in the
encrypted offline archive; the external credential directory is not. Restore
that directory from its own protected copy or reconcile its state before
rotation. A mismatched receipt/file pair is refused. The live drill restores an
active workload and verifies it through the actual contained secret client.

Provisioning needs administrative rights to the selected mounts, policy, role
and KV path. Rotation needs policy/role reads, the role's SecretID lookup/create/
destroy endpoints, and `sys/capabilities` verification. Revocation additionally
needs policy update and role deletion. These are operator permissions: a token
allowed to edit ACL policies is powerful. Use independently managed operator
authentication; keeping a bootstrap root token for routine rotation is unnecessary.

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
backup. Use the offline recovery procedure below and preserve unseal custody.

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

## Encrypted offline backup and restore

`scripts/secrets-recovery` stops and removes the owned container before reading
its complete storage. This follows OpenBao's preference for
[offline backups](https://openbao.org/docs/concepts/storage/). It is specific to
this single-node deployment, preserving its Raft node ID, project name, hostname
and network name. Use OpenBao's cluster recovery procedures for a topology with
additional peers. The helper uses no operator token or decrypted unseal share,
and keeps the normal service's request limits intact.

Arrange a maintenance window, settle operations, and stop separate executors
before backup. The helper removes only its OpenBao container; it cannot establish
the outcome of a receiver action. Keep a coordinated shop/Keycloak backup and
external-system recovery records separately. The OpenBao archive contains Raft
storage, audit files, TLS material, configuration, cluster identity, and the
encrypted bootstrap packets. It excludes PGP private keys, decrypted shares,
external workload credential files, and container diagnostic logs.

Create private backup/key directories outside deployment state. Store a copy of
the encryption key separately from the archives and retain the original PGP
custody. The outer archive key does not replace the unseal quorum.

```sh
install -d -m 700 /secure/chipload-backups /secure/chipload-recovery-keys
uv run --extra server python scripts/secrets-recovery keygen \
  --key /secure/chipload-recovery-keys/openbao.key
uv run --extra server python scripts/secrets-recovery --state .local/secrets backup \
  --key /secure/chipload-recovery-keys/openbao.key \
  --archive /secure/chipload-backups/openbao-checkpoint.gpg
```

Use a new archive filename for each backup. A successful backup leaves OpenBao
stopped. Start it with `secrets-stack up` and submit the original threshold shares
when maintenance is complete. If backup fails, its durable marker blocks startup
and unsealing. Repeat the same backup command with the same key and archive path;
an already-published archive is authenticated before completing the interrupted
backup. Do not remove the marker to bypass an unfinished copy. A token revoked
before backup remains revoked in the restored store.

Restore requires an absent destination directory and no container from the
original Compose project. If the original host still exists, fence its service
before bringing up a replacement; local checks cannot detect another Docker
host. On a replacement host, recreate the same internal network and use the
backed-up pinned image and Compose contract. The helper refuses an existing
destination, conflicting project, shared storage mounts, changed deployment
contract, unauthenticated archive, links, unexpected paths, or altered contents.

```sh
uv run --extra server python scripts/secrets-recovery --state .local/secrets-restored restore \
  --key /secure/chipload-recovery-keys/openbao.key \
  --archive /secure/chipload-backups/openbao-checkpoint.gpg
uv run --extra server python scripts/secrets-stack --state .local/secrets-restored up
uv run --extra server python scripts/secrets-stack --state .local/secrets-restored unseal \
  --share-file /secure/custodian-share
```

Each custodian submits a distinct original share. OpenBao starts sealed and its
cluster identity is checked after unsealing. Verify a permitted workload read,
denied cross-shop access, and the receiver's current credential before resuming
executors. Review rotations and revocations made after the backup: recovery
returns secrets and policies to the backup time, while receiver credentials,
external actions and wall-clock expiry remain independent. Do not use a restored
store as a second active copy of the same authority.

Restore authenticates and validates a private staged copy, then atomically
installs the complete offline directory. Failure before installation leaves the
destination absent and can be retried. If completion output is lost after
installation, inspect `recovery.json` and the service's status; restore never
overwrites that directory. Backups allow up to 10,000 entries and a 64 GiB archive.
Allow approximately three archive sizes of working space for backup and two for
restore. GnuPG protects the archive; temporary plaintext needs protected host
storage. An abrupt host/process failure can leave private `.secrets-backup-*`
or `.secrets-restore-*` staging directories; remove those only after confirming
no recovery command is active. Off-host retention, key custody, free space and
replacement-host recovery need operator drills.

## Verify

```sh
./scripts/build-runtime
CHIPLOAD_SECRETS_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_secrets_stack_live.py' -v
CHIPLOAD_SECRETS_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_secrets_recovery_live.py' -v
CHIPLOAD_SECRETS_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_secrets_workload_live.py' -v
```

The live test creates temporary TLS/PGP material and a fresh private network. It
checks actual container limits and mounts, encrypted bootstrap/decryption,
interrupted local completion, duplicate-share rejection, distinct-share unseal,
scoped AppRole permissions, audit redaction and the real Chipload secret-fetch
client in a restricted container. It replaces CA/leaf certificates, rejects old
trust, verifies sealed restart and secret persistence, and refuses missing or
mismatched initialized storage. Test-owned containers, network and temporary
files are removed. Custody, production certificates, off-host recovery and
operational monitoring need deployment-specific verification. The recovery drill
interrupts backup before copying and after archive publication, deletes the
original deployment, restores fresh storage, and unseals with the original
quorum. It checks the original cluster ID, stored receiver secret, AppRole
permissions, preserved root-token revocation, audit history and a second backup
of the restored deployment.

The workload drill provisions an exact-path identity, checks real client access,
and injects lost responses after issuance, credential installation and retirement.
It verifies rotation with a non-root administrative token after bootstrap-root
retirement, restores an active workload, and revokes both new logins and an
already-issued token's read access. Ordinary tests cover drift, private paths,
failed installation, expired pending credentials, missing local files and safe
diagnostics.
