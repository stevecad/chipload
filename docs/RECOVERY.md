# Back up and restore a TLS deployment

`scripts/tls-recovery` creates an encrypted recovery set containing the paired
Chipload databases, Keycloak database, operator grants, client configuration,
TLS certificates and keys, and deployment secrets. It can restore that set into
a fresh state directory and PostgreSQL volume. Application services stay stopped
until the operator starts the restored deployment.

The recovery set preserves the configured client listener address. Starting a
restored deployment with a non-loopback address exposes its client port again;
confirm the destination host's address, DNS and firewall before startup.
Administration remains bound to loopback. Older state without a listener setting
defaults to loopback.

The latest credential-rotation and signing-key receipts are included when present.
The identity database preserves active and retained verification keys; a retired
provider stays deleted after restoration from a later backup. Backups reject
unfinished [credential rotations](CREDENTIALS.md); finish the pending cutover
before taking a new recovery set.

Backups also reject unfinished [OpenBao operator identity changes](SECRETS.md#keycloak-operator-login).
The Keycloak database holds those users, clients and role assignments; their
OpenBao policies, JWT trust and local grant receipts live in the separate secrets
archive. Coordinate both recovery points and reconcile later revocations and key
changes before restoring operator access.

Recovery sets include schema-8 container launch records, operation bindings, Python run history, execution history, and
simulation receipts. Backups reject queued, running, or indeterminate operations.
Stop new submissions, finish or cancel queued calls, reconcile uncertain outcomes,
and stop separately launched executors before backup. The stack helper supervises
its API/build/Python broker; it does not stop an independently launched executor.
See [operation recovery semantics](OPERATIONS.md#states-cancellation-and-recovery).
Restoring these databases cannot undo an equipment action or an ERP transaction;
external receiver-state recovery remains integration work. The separate
[executor launcher](ADAPTERS.md) honors the TLS deployment operation lock and
incomplete-state markers, but the stack helpers do not stop its Compose project.
Its adapter configuration, AppRole credential directory, OpenBao state, and
receiver ledger need their own coordinated protection and recovery. Use the
[OpenBao offline recovery helper](SECRETS.md#encrypted-offline-backup-and-restore)
for the private secrets deployment during the same maintenance window. It
preserves that service's storage and custody separately from this archive.

This workflow covers the [single-host TLS stack](TLS.md). It does not back up
OpenBao, ERP systems, equipment controllers, external evidence stores, Docker
images, or the repository checkout. Preserve those independently. Disaster
recovery across a lost host and an external secrets service remains an
operational qualification task.

## Prepare a recovery key

Use the deployment's non-root owner, Python 3.12+, `uv`, Docker Compose, and
GnuPG 2.4 or later. The helper uses an isolated GnuPG home, no interactive agent,
and no passphrase cache. It supplies the key through a private file path, never
through a command-line value or an environment variable.

```sh
uv run --extra server python scripts/tls-recovery keygen \
  --key .local/recovery-keys/shop.key
```

The key is 32 random bytes encoded as hexadecimal, stored with mode 0600 inside
an owner-only directory. Existing files and symlinks are rejected. Keep a separate
protected copy outside the deployment host and separate from stored backups.
The key permits reading and creating recovery archives; it is operator authority
and does not belong in agent connection settings, source projects, or MCP.

The archive uses GnuPG's OpenPGP AES-256 symmetric encryption and modification
detection. Decryption must succeed and report integrity verification before the
helper reads its manifest or uses any contents. There is no plaintext fallback.
See GnuPG's [unattended operation options](https://gnupg.org/documentation/manuals/gnupg24/gpg.1.html).

## Create a backup

```sh
uv run --extra server python scripts/tls-recovery --state .local/tls backup \
  --key .local/recovery-keys/shop.key \
  --archive .local/backups/checkpoint.gpg
```

Choose a new archive filename each time. The destination directory must be
owner-only; the helper creates it with mode 0700 if absent. Both the archive and
recovery key must be outside deployment state. Only a complete encrypted file
is installed at the final path, without overwriting an earlier backup.
Keep archive and key files in owner-only directories when inspecting or
restoring them as well; aliases cannot place recovery material inside state.

Run against either a healthy complete stack or a stopped stack whose Keycloak
volume still exists. Stop unmanaged API/broker/local writer processes and avoid
editing settings during the operation. Keep the checkout and image references
fixed. The helper verifies project ownership, mounts, running images, private
state, database pairing, and image fingerprints before collecting data.

The backup pauses the gateway and API, waits for the broker's graceful shutdown,
and stops Keycloak. PostgreSQL runs privately while `pg_dump` captures identity
data. Any remaining running build makes backup fail; finish it or use the
documented build recovery procedure first. Queued builds remain queued in the
snapshot and can run when a restored broker starts.

Both SQLite writer locks are held while their snapshots are taken with
[SQLite's backup API](https://www.sqlite.org/backup.html). Committed WAL data is
included. Source revisions, reports, publication generations, observations,
audit records, quota generations/history, and spent request allowances are
preserved. Keycloak uses a [custom-format PostgreSQL dump](https://www.postgresql.org/docs/17/app-pgdump.html),
including its subjects, clients, role mappings, and signing keys. Its application
writer remains stopped throughout that snapshot window.

Snapshot files are self-contained SQLite databases. Runtime initialization
reenables WAL on both restored stores before serving requests, preserving policy
history and spent credit. The recovery drill verifies this with the actual API
and broker starting against the restored pair.

The helper restores the previous running/stopped state before packing and
encrypting the snapshots. This causes a maintenance interruption; it is not an
online backup protocol. A failed snapshot also attempts to restore the previous
service state. Backup policy, off-host copies, retention, and a recovery-point
target must be chosen by the operator. The tool does not schedule or upload
backups.

Private temporary files contain plaintext databases and credentials during
backup and restore. Provision enough protected disk space for snapshots, an
uncompressed archive, and the encrypted output. Use encrypted host storage for
protection against disk theft. Normal completion and handled failures remove
temporary directories; abrupt process or host termination can leave them behind.
Deletion is not a secure-erasure guarantee.

The default archive/content bound is 64 GiB. `--max-bytes` accepts 1 MiB–1 TiB;
increase it deliberately for a larger deployment. GnuPG also bounds decrypted
output. Database dump/restore and encryption commands have execution deadlines.
Free-space monitoring remains an operator responsibility.

## Inspect and restore

Inspect authenticates the encrypted file and displays its manifest metadata,
including backup ID, time, checkout, image references, runtime fingerprints,
and file sizes. It does not print source, database rows, or secret values, and
does not require Docker:

```sh
uv run --extra server python scripts/tls-recovery inspect \
  --key .local/recovery-keys/shop.key \
  --archive .local/backups/checkpoint.gpg
```

Use the matching SDK, dependency lock, and deployment files for restore; an
incompatible checkout is rejected before a target deployment is created.
The manifest records the Git commit and whether the working tree was dirty.
A dirty checkout requires preserving its actual inputs as well as the commit.
Keep the pinned images available or rebuild them from matching inputs. Recovery
does not silently combine a restore with an application/schema upgrade.

```sh
uv run --extra server python scripts/tls-recovery --state .local/recovered restore \
  --name chipload-recovered \
  --key .local/recovery-keys/shop.key \
  --archive .local/backups/checkpoint.gpg
```

The state path and project name must be new. Restore refuses existing deployment
state, containers, or volumes. The archive is authenticated before any extraction;
only known regular-file names are accepted. Links, traversal, repeated members,
missing members, mismatched sizes/hashes, incompatible database schemas, broken
references, and mismatched capacity stores are rejected.

Restore retains the issuer, audience, hostnames, ports, shop IDs, and subject
bindings. It chooses an unused private Docker subnet, updates the gateway's exact
trusted IP, and rewrites client file paths for the new state directory. It restores
PostgreSQL into a new, explicitly identified volume using a
[single transaction](https://www.postgresql.org/docs/17/app-pgrestore.html).
Only the private database service runs during this process. A `restore.json`
receipt records the backup ID, archive digest, time, and local operator UID.

The restored service uses the backed-up authorization and secrets. Review changes
made since that backup, including deleted identities, revoked credentials, and
removed grants, before exposing it. Check certificate expiry and replace expired
material using the TLS guide. Keep one active deployment for a given issuer and
resource URL; source and restored stacks retain the same host ports.

For a local cutover after reviewing restored state:

```sh
uv run --extra server python scripts/tls-stack --state .local/tls down
uv run --extra server python scripts/tls-stack --state .local/recovered up
```

The first command preserves the source's state and database volume. The second
verifies the restored HTTPS listeners and issuer. Query a known publication,
obtain a fresh agent token, verify its grants, and check a forbidden cross-shop
request. Restoring files alone does not prove operational recovery.

## Handle interruption

Deployment changes and recovery commands share an exclusive operation lock.
`tls-stack up` and `reload` reject incomplete backup, restore, or credential-rotation markers. Status,
logs, and shutdown remain available once the active operation releases its lock.
Do not remove a marker merely to start a partially restored service.

If backup was abruptly terminated, restore the previous service state with:

```sh
uv run --extra server python scripts/tls-recovery --state .local/tls resume-backup
```

This resumes services or leaves them stopped according to the recorded starting
state. It does not turn a partial archive into a successful backup. The marker
records its temporary directory; inspect it before resuming and remove only that
operation's leftover private staging files afterward. An existing final archive
can be checked with `inspect` and a restore drill.

If restore fails after preparing its new state, rerun with the same authenticated
archive and project name:

```sh
uv run --extra server python scripts/tls-recovery --state .local/recovered restore \
  --name chipload-recovered --resume \
  --key .local/recovery-keys/shop.key \
  --archive .local/backups/checkpoint.gpg
```

The marker binds the attempt to the archive digest and its own PostgreSQL volume.
Application startup remains blocked. A retry replaces database contents within
one transaction, including when the preceding attempt committed but stopped
before marking recovery complete. Changes to staged shop data or settings cause
resume to fail; preserve that state and restore into another fresh directory.
Restore never silently adopts another project's database volume.

## Exercise recovery

```sh
CHIPLOAD_RECOVERY_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_recovery_live.py' -v
```

The live drill publishes a shop through authenticated REST and an isolated broker,
records observations and spent capacity, and backs up the real TLS/Keycloak stack.
It changes the source deployment afterward so the restored point is distinguishable.
It abruptly terminates another backup, verifies startup refusal, and resumes the
original service. It then simulates interruption after PostgreSQL restore commits,
verifies the restored application stays offline, and retries the same recovery.

After startup, it checks fresh Keycloak authentication, retained signing keys and
shop grants, source/build/publication/observation/audit state, preserved policy and
spent request credit, forbidden cross-shop access, REST/MCP discovery parity, and
a new authorized publication change. Unit checks also cover uncheckpointed WAL,
writer contention, wrong keys, tampered ciphertext, unsafe archive members,
private file handling, and refusing to overwrite state. Test cleanup is restricted
to each drill's own containers, volumes, and temporary directories.

Python runs must settle before backup: queued or running code jobs are rejected. Cancel them or let the broker finish, then retry. Code run histories, call journals and resolved container launch records are preserved by schema-8 backups. Unresolved creation intent blocks backup even when the container is currently absent; do not discard it to make a backup pass.
