# Upgrade the private OpenBao image

`secrets-recovery restore --upgrade-image openbao-2.7.1-patched-build` upgrades the
original pinned OpenBao 2.7.1 deployment to Chipload's patched 2.7.1 build through
an encrypted backup and a fresh state directory. It preserves the node name,
cluster, TLS material, unseal quorum, secret store, policies, credentials and
revocations. The deployment remains stopped until the owner starts and unseals it.

This is an explicit maintenance operation. Stop dependent adapters and finish
pending identity/workload changes first. Keep the original checkout, image,
encrypted backup and independent custody material until the upgrade and recovery
drill have been verified. The supplied deployment is single-node and needs a
maintenance interruption.

## Supported transition

The approved source is the original registry image
`ghcr.io/openbao/openbao:2.7.1@sha256:6d2b93856e3fcf7b18ad855a0b51eaba474dc8b79cf554379ea32034797d2acf`
with its exact original Compose contract. The target is the patched source build
documented in [SECRETS.md](SECRETS.md), using the reviewed recipe and module locks.
Both endpoints retain the same upstream OpenBao release. The exact source and
target image/Compose fingerprints are in
[`scripts/secrets_upgrades.py`](../scripts/secrets_upgrades.py).

The helper requires that exact pair and the named flag. Normal restore requires
the backup's existing image contract. Unreviewed images, changed Compose files,
downgrades and another invocation of this transition on an already-upgraded
backup are refused. A future build change needs its own reviewed transition and
live drill; the current flag does not authorize arbitrary future upgrades.

Build the target from the current checkout before stopping the existing service:

```sh
./scripts/build-secrets
```

This verifies or builds the target image without changing the running deployment.
Keeping compilation outside the maintenance window reduces downtime.

## Back up with the original checkout

Use the checkout that manages the existing deployment, such as revision
`d0a457b6d231584b671ad399913ee7a5f13e3f9b` for this source image. Its recovery writer
uses the same archive format. Run as the non-root deployment owner, using private
canonical paths and the original network. Keep the encryption key and backup
outside service state, under independent custody. The examples assume those
private directories and the backup key already exist; see
[encrypted backup setup](SECRETS.md#encrypted-offline-backup-and-restore).

From the original checkout:

```sh
uv run --extra server python scripts/secrets-recovery \
  --state /srv/chipload/secrets-original backup \
  --key /secure/backup-key --archive /secure/secrets-before-upgrade.gpg
```

Backup removes the owned container and verifies that no other container mounts
the source store. It leaves the source directory intact. Finish an interrupted
backup with the same old checkout, key and output path before proceeding. Keep
the old service stopped; two copies must not run as the same cluster or network
identity. Pending initialization, workload or operator-identity changes must be
resolved before backup.

## Restore with the current checkout

Use a fresh destination with an owner-only parent. The original internal network
must exist. From the current checkout:

```sh
uv run --extra server python scripts/secrets-recovery \
  --state /srv/chipload/secrets-patched restore \
  --key /secure/backup-key --archive /secure/secrets-before-upgrade.gpg \
  --upgrade-image openbao-2.7.1-patched-build
```

Restore authenticates the archive before extraction and validates every file,
the original contract, custody metadata and cluster receipt. It verifies the
target build before installation, refuses an existing project or storage user,
and changes only the deployment's image selection and local ownership receipt.
It records the transition, archive digest and cluster in `recovery.json`, then
installs the completed directory atomically. Raft data, audit history, encrypted
bootstrap material and completed workload/operator receipts are copied intact.
The original directory and encrypted archive are unchanged.

If interrupted before installation, rerun against the still-fresh destination.
If the destination exists after a lost response, preserve it: inspect
`deployment.json`, `initialized.json` and `recovery.json` against the intended
target, source archive digest and original cluster. A completed installation is
not overwritten by retry. Resolve an unexpected directory before startup; do not
edit its recorded image or delete initialized storage to force a retry.

## Start, verify and resume clients

```sh
uv run --extra server python scripts/secrets-stack \
  --state /srv/chipload/secrets-patched up
uv run --extra server python scripts/secrets-stack \
  --state /srv/chipload/secrets-patched unseal --share-file /secure/custodian-share
```

Each custodian follows the existing share procedure until the original threshold
is met. The server's cluster ID must match the restored receipt. Verify ordinary
operator login, each required workload's exact-path access, cross-workload
denials, known revocations and audit recording before resuming dependent adapters.
The service hostname and network stay the same; use the new state path for future
operator commands, backup jobs and local maintenance automation. Retain the same
external credential files and the matching Keycloak deployment.

This operation preserves the existing operator authentication contract. An older
version-1 identity receipt still needs the separate, explicit
[`enable-mfa` procedure](SECRETS.md#keycloak-operator-login).

Capture a new backup using the current checkout and upgraded state. That archive
uses the current contract and restores normally, without `--upgrade-image`.
Rehearse it before retiring the old recovery set.

## Rollback and limits

Rollback requires the original backup and matching old checkout/image. Stop and
remove the upgraded container first, then use the old recovery command to restore
the original archive into another fresh private directory. Reconcile credentials,
policies, revocations and external changes since the backup before resuming any
client. Retaining old data is a recovery option, not permission to run both copies.
Replacing an upgraded binary with an older binary does not establish storage
compatibility; [OpenBao's upgrade guidance](https://openbao.org/docs/guides/upgrade/)
requires restoring the corresponding data when rolling back.

This transition qualifies the private TLS service's original 2.7.1 packaging
change on one host. Other OpenBao releases, custom configurations, development
stack state, replacement-host execution and broader coordinated application
upgrades need their own qualification.

The recovery live suite starts the actual pinned older image with the original
Compose file, initializes its encrypted Raft store, provisions scoped credentials
and revokes root. It interrupts the old-format backup, removes the original
store, refuses an ordinary restore, applies the explicit upgrade through the CLI,
unseals with the original shares and checks secrets, ACL denials, revocation and
audit preservation. A second backup and ordinary restore of the upgraded service
also pass. Unit checks cover exact-contract refusal, target-build verification,
unchanged source state and interruption on both sides of atomic installation.
