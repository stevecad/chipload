# Rotate TLS deployment credentials

`scripts/tls-credentials` rotates the provisioned `chipload-agent` client secret,
the PostgreSQL password used by Keycloak, or the Chipload realm's RS256 signing
key. It preserves client and subject IDs, shop grants, and shop data. This is a local operator tool
for the [TLS deployment](TLS.md); it is not an agent permission or REST/MCP action.

Use the non-root deployment owner, the matching checkout and images, and a
healthy running stack. Keep other administrative changes and configuration edits
out of the rotation window. The command checks container ownership and mounts,
private file permissions, and the existing credentials before recording a change.

## Rotate the shop agent

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls rotate-agent
```

By default this authenticates the initial `admin` operator with the private
`secrets/admin-password` file. To use a managed master-realm operator account,
pass `--operator-user NAME --operator-password-file PATH` before the action.
The password file and its parent directory must be owner-only. This authentication
path uses the password grant for `admin-cli`; it requires an account permitted
to use that grant and administer the Chipload client. Managed operator onboarding,
MFA, and rotation of operator passwords remain identity-administration work.
Alternatively, use `--operator-token-file PATH` with an already-issued master-realm
administration access token from your managed login flow. The token and its parent
directory must be owner-only; the helper never stores it in the rotation journal.
If it expires during an interrupted operation, obtain a fresh authorized token
and resume. Token-file and password-file options are mutually exclusive.

The helper connects to the local administration listener with the configured
hostname and CA verification. It updates the existing client through the
[Keycloak Admin API](https://www.keycloak.org/docs-api/26.8.1/rest-api/index.html),
invalidates any retained previous client secret, obtains a new agent token, and
compares the API's effective identity and grants. The old secret must fail token
issuance with a client-authentication rejection before the local secret and realm
import are updated.
No client or user is recreated, and shop bindings are not edited.

The new secret is stored at the existing `secrets/agent-secret` path with mode
0600. Distribute it to authorized agent clients through their protected credential
delivery mechanism; it is never printed. `client.json` keeps the same file path.
Clients holding the old secret must reload their credentials to request tokens.
There is no grace period for issuing tokens with the previous secret.

Already-issued access tokens remain valid until their expiry because Chipload
verifies JWTs locally. Secret rotation is not immediate token revocation. An
incident requiring immediate loss of access also needs removal of the affected
subject's Chipload grants and API reconfiguration, together with Keycloak
administration. Do not interpret a rejected old secret as invalidation of every
token previously issued to it.

## Rotate the identity database password

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls rotate-database
```

This operation uses the deployment owner's Docker authority and the database's
local maintenance socket. It does not use a Keycloak operator password. Keycloak
stops to close its database sessions; PostgreSQL changes the existing role's
password using [`psql`'s `\password` command](https://www.postgresql.org/docs/17/app-psql.html).
That command encrypts the password before sending `ALTER ROLE`, keeping plaintext
out of SQL statement logs. Passwords enter `psql` through stdin, not command-line
arguments or Docker environment configuration.

The helper checks both passwords against the private TCP database address, where
password authentication applies. It requires the new one to succeed and the old
one to fail specifically because authentication was rejected. Connection errors
do not count as credential rejection.

It atomically replaces `secrets/db-password` and recreates PostgreSQL and Keycloak
so their individual file mounts read the new file. The password keeps mode 0444
inside its owner-only parent, as required by the PostgreSQL container UID. The
existing volume, role, and database are preserved. A fresh agent login through
the API must return the same identity and grants before rotation completes.

Plan an identity-service interruption. Public discovery and the API/broker remain
running, but authentication that needs Keycloak or a fresh verification key can
fail during reconnection. This is a maintenance cutover, not zero-downtime rotation.

## Resume an interrupted cutover

Before the first external change, the command fsyncs an owner-only
`.credentials-incomplete.json` journal containing the selected rotation and its
retry intent. Password rotations include the previous and pending credentials;
signing rotations record provider/key IDs and phases, without exporting private
keys. Preserve it after failure. While it exists, `tls-stack up`, `reload`,
and recovery backups refuse to proceed. The normal operation lock also prevents
concurrent deployment/recovery commands during rotation.

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls resume
```

For an agent or signing rotation, supply the same operator options or another authorized
operator account when resuming. Retry reuses the pending secret even if the
remote change committed before its response was lost. Changes to deployment
settings, the identity volume, the configured subject/client, or an unrelated
credential value cause refusal rather than overwriting another operator's work.
Do not remove the journal to bypass this check, move state during rotation, or
manually restart services against mismatched files. The command never rolls a
committed credential back to the previous value.

After verification, `credentials.json` records the latest completed rotation's
ID, kind, timestamps, and local operator UID without secret values. It is a latest
operation receipt, not a complete administrative audit history. The journal is
then removed. Protected host storage is still required; deletion is not secure
erasure and an abrupt exit may leave private temporary files.

Take a new [encrypted backup](RECOVERY.md) after rotation. It includes the current
secrets, identity database, realm import, and receipts. A backup from before a
rotation contains the earlier credentials; review and rotate them before exposing
a restored service. This helper does not rotate master-realm signing keys, operator
passwords, TLS certificates, OpenBao credentials, or external adapter secrets.

## Rotate the realm signing key

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls rotate-signing
```

The helper creates a 3072-bit generated RSA provider inside Keycloak for RS256,
with a recorded provider ID and a higher priority. It verifies the new key and
a fresh agent token through the actual Chipload API before making the previous
provider passive. Both public keys stay in JWKS during the overlap; existing
unexpired tokens retain their identity and grants. The private keys stay in the
identity database and its encrypted recovery archives. Password/client-secret
rotations do not change these keys.

This uses Keycloak's [generated providers and passive keys](https://www.keycloak.org/docs/latest/server_admin/#rotating-keys)
and [component administration API](https://www.keycloak.org/docs-api/26.8.1/rest-api/index.html#_component).
It supports the generated RSA provider used by the supplied deployment. External
keystores, other signing algorithms, master-realm keys, and concurrent identity
administration require their own procedures. Keep unrelated key changes out of
the rotation window; changed key IDs, provider settings, or the active selection
cause refusal. The operator needs realm view/manage permission.

The default verification overlap is **60 days**, starting after the previous
provider becomes passive. `--retain-days N`, before `rotate-signing`, accepts
1–365 days. `signing.json` records both provider/key IDs, activation time and the
earliest retirement time as Unix seconds. The helper never retires keys merely
because time passed. Plan the interval for every client of this realm: old
cookies, refresh tokens and offline tokens may still rely on the previous key.
Those clients must refresh or reauthenticate before retirement. The interval
does not prove all clients have updated.

The API's bounded unknown-key refresh can briefly reject a newly issued token
for up to five seconds after activation. Verification retries the identity read
through that window; it never retries shop mutations. This is a managed rotation
with verification, not a guarantee of uninterrupted authentication for all clients.

After the recorded retention deadline, retire the previous key explicitly:

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls retire-signing
```

Retirement disables the recorded old provider, recreates the API to discard
cached verification keys, verifies new-token authentication and removal of the
old public key, then deletes that old provider. Plan a brief API interruption.
Other clients manage their own JWKS caches. Tokens or sessions still using the
old key can stop working. A new signing rotation is refused until the recorded
previous key is retired, keeping outstanding retirement work explicit.

The [OpenBao operator login](SECRETS.md#synchronize-signing-keys) uses pinned public
keys on its isolated network. Obtain a fresh OpenBao administrator token before
activating a realm key, then run `secrets-identity sync-keys` while that token is
valid. Repeat synchronization after retirement. These signing commands do not
update OpenBao automatically; new-key login fails until synchronization, and a
retired key remains trusted there until removed. Both backup and credential
rotation refuse an unfinished secrets-identity change; resume it first.

Both commands use the same private journal, deployment lock and `resume` command
as credential rotation. Lost create/update/delete responses are reconciled
against the recorded provider and key IDs; resume does not generate another key.
The retention interval cannot be shortened during resume. Backups preserve
`signing.json` and the actual Keycloak keys; a retired provider stays absent after
restore. Earlier backups can contain earlier keys, so coordinate their retention
and access with incident recovery. Use the explicit early-revocation command
below when the recorded previous key must lose trust before that deadline.

## Revoke a previous signing key early

After a replacement key has been activated and verified, the operator can revoke
the exact previous key without waiting for its retention deadline:

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls \
  --key-id PREVIOUS_KEY_ID revoke-signing
```

Read `old_kid` from the protected `signing.json` receipt and verify it against your
incident's trusted key inventory. The command requires that exact ID, an intact
overlap receipt, the recorded active replacement and a passive previous provider.
It refuses the active replacement or an unrelated key. `retire-signing` keeps its
normal deadline; changing `--retain-days` cannot shorten an existing overlap.

The helper records its intent before changing services, stops the API and
confirms it is stopped, then disables the previous Keycloak provider. This removes
the API process that may have cached the compromised public key. After disabling
the provider, the helper recreates the API, verifies fresh-token authentication
with unchanged grants and the old key's absence from JWKS, then deletes the old
provider. Public discovery and authenticated API calls are interrupted during
this cutover. Other realm clients must refresh their own verification-key caches.

If the disable response is lost, the API stays stopped. Keep the journal and run
the ordinary `resume` command, without `--key-id`. Resume uses the recorded ID and
does not generate another signing key or restart the retention interval. Startup
and backup remain blocked while the journal exists. If stopping the API cannot
be confirmed, the helper refuses to disable a provider and reports the failure;
it has not established containment. Operator changes outside the deployment lock
must remain suspended throughout the cutover.

The completed `signing.json` keeps status `retired` and adds `revocation` with the
exact `key_id`, `requested_at` and `completed_at` timestamps. `credentials.json`
records kind `signing-revoke`. Encrypted backups preserve both the receipt and
the Keycloak database with the provider removed. A backup predating revocation
can restore the compromised key; reconcile incident history before exposure.

For a compromise of the currently active key, first isolate public client traffic
using the deployment's network controls while retaining operator access. Prepare
a verified replacement with `rotate-signing`, then revoke its recorded previous
key. If an earlier rotation is still in overlap, resolve that previous key first
with its exact ID, then rotate and revoke the affected active key. Keep public
access contained during the sequence. This helper does not coordinate that
network containment or determine which key was compromised.

OpenBao's pinned JWT keys remain a separate trust store. Obtain an independent,
usable OpenBao administrative token and run
[key synchronization](SECRETS.md#synchronize-signing-keys) after activation and
again after revocation. Until synchronization removes the old key, OpenBao still
trusts it. Existing OpenBao tokens retain their own lifetime and policy; review
and revoke affected tokens or grants through OpenBao incident procedures.
Do not treat Chipload's successful revocation receipt as revocation everywhere.

Already-admitted Python/build/operation jobs and completed effects are not undone
by key revocation. Review and contain suspected jobs and external effects
separately. A compromised host, Keycloak administrator or master-realm key also
requires broader recovery than this Chipload-realm command provides.

## Verify

```sh
CHIPLOAD_CREDENTIAL_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_credentials_live.py' -v
CHIPLOAD_CREDENTIAL_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_signing_live.py' -v
```

The live drill publishes a shop, interrupts immediately after each remote
credential commit, verifies startup/backup gating, and resumes. It checks old
credential rejection, unchanged identity/grants, source/publication/catalog
preservation, and cross-shop denial. It then backs up and restores the rotated
deployment and checks the new credentials and signing keys again. Tests use
temporary state and remove only their own containers and volumes.

The scheduled-retirement drill loses a provider-creation response, verifies both
generations of tokens and cross-shop denial during overlap, rejects premature
retirement, and loses the disable response. It advances only the operator retention clock
to exercise retirement while the original JWT is still unexpired. Actual API
rejection then proves key removal rather than token expiry. It backs up and
restores the identity database and retirement receipt, checking that the old key
remains absent. Unit checks cover activation/passivation/deletion interruptions,
changed-provider refusal, retention, and bounded verifier-cache behavior.

The emergency drill uses the real retention clock, rejects a wrong target ID,
loses the provider-disable response and confirms the API is actually stopped.
After resume, the previously accepted, still-unexpired token is rejected and a
new token keeps the original grants. Backup/restore preserves the revocation
receipt and the key's absence. This verifies the supplied Keycloak/Chipload stack;
it does not exercise other clients' caches or a coordinated OpenBao incident.
