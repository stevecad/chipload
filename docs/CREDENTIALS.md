# Rotate TLS deployment credentials

`scripts/tls-credentials` rotates the provisioned `chipload-agent` client secret
or the PostgreSQL password used by Keycloak. It preserves client and subject IDs,
shop grants, identity signing keys, and shop data. This is a local operator tool
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
`.credentials-incomplete.json` journal containing both credentials and the selected
rotation. Preserve it after failure. While it exists, `tls-stack up`, `reload`,
and recovery backups refuse to proceed. The normal operation lock also prevents
concurrent deployment/recovery commands during rotation.

```sh
uv run --extra server python scripts/tls-credentials --state .local/tls resume
```

For an agent rotation, supply the same operator options or another authorized
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
secrets, identity database, realm import, and receipt. A backup from before a
rotation contains the earlier credentials; review and rotate them before exposing
a restored service. This helper does not rotate Keycloak signing keys, operator
passwords, TLS certificates, OpenBao credentials, or external adapter secrets.

## Verify

```sh
CHIPLOAD_CREDENTIAL_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_credentials_live.py' -v
```

The live drill publishes a shop, interrupts immediately after each remote
credential commit, verifies startup/backup gating, and resumes. It checks old
credential rejection, unchanged identity/grants, source/publication/catalog
preservation, and cross-shop denial. It then backs up and restores the rotated
deployment and checks the new credentials and signing keys again. Tests use
temporary state and remove only their own containers and volumes.
