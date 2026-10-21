# Back up and restore an ACME issuer

`scripts/tls-acme-recovery` encrypts the issuer's account key and registration,
client storage, configuration, trust, certificate generations and receipts. A
restore preserves that account and prepares private state at a fresh path.
Certificate delivery starts disabled; the owner must bind the recovered issuer
to the intended deployment.

This archive is separate from the [TLS deployment recovery set](RECOVERY.md).
Keep both when using [ACME renewal](ACME.md). Generated systemd units and runtime
lock files are excluded: units contain the old checkout and state paths and must
be regenerated. Preserve any locally customized timer policy separately.

Use the non-root issuer owner, the locked Python server environment, GnuPG 2.4+
and a local Docker daemon. Restore needs a Linux libc and filesystem supporting
[`renameat2` with `RENAME_NOREPLACE`](https://man7.org/linux/man-pages/man2/rename.2.html).
It will not fall back to overwriting an existing directory. Use the backed-up
issuer image pin; image upgrades are a separate operation.

## Create and verify a backup

Make private directories for the encryption key and archives. The key must stay
outside issuer state and separate from encrypted archives. Keep an additional
protected off-host copy of the key; losing it makes these archives unusable.

```sh
install -d -m 700 "$HOME/.config/chipload-recovery" .local/issuer-backups
CHIPLOAD_ACME_KEY="$HOME/.config/chipload-recovery/acme.key"
uv run --extra server python scripts/tls-acme-recovery keygen --key "$CHIPLOAD_ACME_KEY"
uv run --extra server python scripts/tls-acme-recovery --state .local/acme backup \
  --key "$CHIPLOAD_ACME_KEY" --archive .local/issuer-backups/checkpoint.gpg
uv run --extra server python scripts/tls-acme-recovery --state .local/acme inspect \
  --key "$CHIPLOAD_ACME_KEY" --archive .local/issuer-backups/checkpoint.gpg
```

Key generation and backup refuse existing outputs. Reuse the existing key and
choose a fresh archive filename for each later backup. `inspect` decrypts and
validates the complete archive, returning its identity, original issuer path and
container name, file count and certificate fingerprint. It does not print key
bytes or register an account, and does not require Docker.

Stop the issuer's timer for the backup window and let an active renewal finish.
The helper takes the same issuer lock as `run`. It refuses a running/restarting
client, an automatic Docker restart policy, another container mounting its state,
or pending issuance/delivery. Finish pending work with the original issuer's
`run` command first. Backup does not stop or remove the persistent issuer
container and does not change account, certificate or deployment bytes. Restart
the timer after a successful backup.

The helper reads a consistent snapshot, validates it, encrypts it with the shared
GnuPG recovery implementation, and publishes the completed archive without
replacing an existing file. It can also preserve a registered account before its
first successful certificate. Established account-key loss or a certificate
store inconsistent with its published bundle causes refusal.

Copy verified encrypted archives to separately managed off-host storage, enforce
retention there, and test retrieving them with the separately held key. Archive
creation alone does not establish that off-host custody works.

## Restore after issuer storage loss

Stop and disable the original renewal timer. Keep the original issuer out of
service, including on another host. On the restoration host, the original issuer
directory must be absent or moved aside, and its container must be retired.
Use `inspect` to identify the original container name, inspect its actual ID and
state, and remove only that confirmed stopped container. Restore refuses retained
original/destination containers and other containers mounting either state path.
It does not delete original state or containers for you.

Create a private destination parent and restore into a new child directory:

```sh
install -d -m 700 .local/issuer-restores
uv run --extra server python scripts/tls-acme-recovery \
  --state .local/issuer-restores/site restore \
  --key "$CHIPLOAD_ACME_KEY" --archive .local/issuer-backups/checkpoint.gpg
```

The entire archive is authenticated before its contents are interpreted. Files
are reconstructed with owner-only permissions in a private staging directory.
After validation, an atomic rename publishes the complete state while refusing
any existing destination, including an empty directory created concurrently.
No client or service is started. `restore.json` records the backup identity and
previous deployment binding; `binding.json` disables automatic delivery.

Review the retained DNS names, CA trust, ACME directory, local listener address
and Docker network for the destination host. Restore preserves these values.
Restore the TLS deployment separately if it was also lost, and finish any pending
deployment recovery. Then explicitly authorize the issuer to manage it:

```sh
uv run --extra server python scripts/tls-acme \
  --state .local/issuer-restores/site bind --deployment .local/site
uv run --extra server python scripts/tls-acme --state .local/issuer-restores/site run
uv run --extra server python scripts/tls-certificates --state .local/site status
uv run --extra server python scripts/tls-acme --state .local/issuer-restores/site timer
```

Follow [scheduling and monitoring](ACME.md#schedule-and-monitor) to install the
new units. Check a real authenticated request and the served certificate before
reenabling recurring work. An expired archived leaf is retained so the same
account can renew it; `run` validates new material before any delivery. Expired
directory trust or a changed CA hierarchy still requires deliberate trust
maintenance. Host/address changes and image migration need separate qualification.

## Interrupted operations and limits

An interrupted backup leaves source state usable. Before publication, the named
archive is absent; after publication, it is complete. If a response was lost,
run `inspect` on that archive instead of replacing it. Restore likewise publishes
complete state or no destination. If its response was lost and the destination
exists, compare `restore.json` with the authenticated archive's backup identity;
do not rerun restore over it.

Plaintext staging is owner-only and normally removed when the command finishes.
A killed process can leave `.acme-backup-*`, `.acme-inspect-*` or `.acme-restore-*`
directories. They contain private keys. Inspect and remove only that operation's
abandoned staging after confirming no corresponding process is using it.

The archive reader accepts at most 2,048 regular files, 1 MiB per file, 64 MiB of
file contents, a 1 MiB manifest, and paths up to 512 characters and twelve
components. GnuPG output is also capped before decoding. Only issuer-owned paths
are reconstructed; hard links, arbitrary symlinks, traversal, conflicting paths,
changed file hashes and extra trailing data are rejected. The known `current`
link is recreated from a validated bundle fingerprint. Historical generations
consume this budget; automatic generation retirement remains separate work.

The encrypted file contains the whole snapshot. Restoring individual keys or
combining registration and certificate files from different recovery points is
outside this workflow. Account rollover, revocation, configuration/image
migration and coordinating a move between actual hosts remain deployment work.

## Verify

```sh
uv run --extra server python -m unittest discover -s tests -p 'test_acme_recovery.py' -v
CHIPLOAD_ACME_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_acme_recovery_live.py' -v
```

The live test requires the pinned ACME images and the built worker/runtime/TLS
images. It uses real GnuPG encryption and a local Pebble CA, kills backup before
and after archive publication, deletes the original issuer directory, refuses a
retained original container, restores the same account key and registration,
then renews a running TLS deployment with unchanged identity/grants. Unit tests
cover authentication failures, bounded inventory, source drift, pending/running
issuers, expired leaves, private permissions, and interrupted restore. The live
drill uses fresh storage on the same host; it makes no public CA requests.
