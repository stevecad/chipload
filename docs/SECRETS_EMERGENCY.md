# Emergency OpenBao administration

Provision and rehearse independent emergency access before retiring the bootstrap
root token. This procedure uses the OpenBao CLI and the supplied
[minimal policy](../deploy/secrets/emergency-policy.hcl). It works without
Keycloak and runs under the deployment owner's authority.

OpenBao 2.7 requires authentication for
[`operator generate-root`](https://openbao.org/docs/commands/operator/generate-root/).
Possession of the unseal shares alone does not satisfy that authentication step.
Keep the deprecated unauthenticated generation endpoints disabled.

The independent login can start, inspect, cancel and contribute to a root-token
ceremony, inspect its own token, and revoke its own token. It cannot read secrets,
change policies, create tokens directly, manage users, or disable auditing.
The configured unseal quorum must still approve generation; the resulting root
token is encrypted to the public PGP key agreed by the custodians. A compromised
emergency password can disrupt a ceremony, so it remains a protected credential.

## Prepare independent custody

Use a high-entropy password stored outside Keycloak, the service state, workload
mounts and shop projects. Retain it in the deployment owner's independent
emergency custody system. Retain the PGP private key and unseal shares under
their separate custody procedures. Losing the password, required shares or
recipient key can prevent this recovery path. Distinct files or keys do not
prove independent human custody.

The following commands run on the trusted deployment owner's workstation or
host, using an OpenBao 2.7 CLI. Keep plaintext working files in an owner-only
directory outside deployment state, with mode `0600`. Disable shell tracing and
terminal recording. Stop on any command failure; do not retry uncertain writes
automatically. No command below requires a secret as a command-line argument.

```sh
umask 077
mkdir -m 700 /secure/emergency
uv run --extra server python scripts/secrets-stack --state .local/secrets status
```

Set these values from that status response. The address is private and may change
after a restart. The TLS name and CA are required even though the address uses a
container IP. Run from a host that can reach that private network; do not publish
the OpenBao listener to make recovery work.

```sh
unset BAO_NAMESPACE VAULT_NAMESPACE VAULT_TOKEN
export BAO_ADDR=https://PRIVATE_CONTAINER_IP:8200
export BAO_TLS_SERVER_NAME=bao
export BAO_CACERT=/absolute/path/to/.local/secrets/certificates/ca.pem
export BAO_MAX_RETRIES=0
export BAO_CLIENT_TIMEOUT=10s
```

Never set `BAO_SKIP_VERIFY`. Unseal the service with the original quorum if it is
sealed, using [the existing share procedure](SECRETS.md#unseal-and-provision-workloads).
The CLI token below is an explicitly authorized bootstrap or full-administrator
token in a protected file. Its environment is visible to the trusted local owner
and host administrators; keep it out of workload processes.

## Provision before retiring root

First inspect `bao auth list` and `bao policy list` with the administrator token.
The names below must be unused for a new setup. If either already exists, inspect
the current mount, user and policy before proceeding; do not overwrite unrelated
configuration. These are manual operator commands, with no Chipload retry journal.

```sh
export BAO_TOKEN="$(cat -- /secure/operator-token)"
bao auth list
bao policy list
python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(48))' \
  > /secure/emergency/password
bao policy write chipload-emergency-root deploy/secrets/emergency-policy.hcl
bao auth enable -path=chipload-emergency \
  -default-lease-ttl=300s -max-lease-ttl=300s userpass
bao write auth/chipload-emergency/users/custodian \
  password=@/secure/emergency/password \
  token_policies=chipload-emergency-root token_no_default_policy=true \
  token_ttl=300s token_max_ttl=300s token_explicit_max_ttl=300s \
  token_type=service token_period=0 token_num_uses=0
bao policy read chipload-emergency-root
bao read auth/chipload-emergency/users/custodian
unset BAO_TOKEN
```

Retain the password before its first remote write. If a write's response is lost,
inspect the actual configuration and test login with that same password before
deciding what remains to do. Do not generate a different password just because a
response was lost. Keep the policy exactly scoped; do not attach other policies
or group authority to this account. Leave
[userpass lockout](https://openbao.org/docs/auth/userpass/#user-lockout) enabled.
The password does not expire automatically; its resulting tokens have a maximum
lifetime of five minutes. This login does not replace normal operator MFA.

Log in, storing only the short-lived session in the protected file. `-token-only`
prevents the CLI from also saving it in its normal token helper.

```sh
bao login -token-only -method=userpass -path=chipload-emergency \
  username=custodian password=@/secure/emergency/password \
  > /secure/emergency/session-token
export BAO_TOKEN="$(cat -- /secure/emergency/session-token)"
bao token lookup -field=policies
bao token lookup -field=ttl
```

Verify the only policy is `chipload-emergency-root`, the TTL is positive and at
most 300 seconds, and known secret reads and administrative writes receive HTTP
403. A connection failure is not an authorization denial. Rehearse the ceremony
below and retire its generated token. Then retire the original bootstrap token
and confirm independent normal and emergency logins still work. Preserve the
encrypted bootstrap packet as a record; decrypting a revoked token cannot restore
its authority.

## Generate and retire an emergency root token

Create a fresh protected working directory for each ceremony; `incident-001`
below is an example name. Coordinate all
custodians before starting: they must agree on the deployment, nonce, purpose and
recipient fingerprint. Export the recipient's public key in binary format; its
private key stays on the custodian's protected workstation.

```sh
mkdir -m 700 /secure/emergency/incident-001
bao operator generate-root -init -format=json \
  -pgp-key=/secure/root-recipient-public.pgp > /secure/emergency/incident-001/attempt.json
bao operator generate-root -status -format=json > /secure/emergency/incident-001/status.json
```

Check `started`, `required`, `nonce` and `pgp_fingerprint` against the expected
threshold, attempt and independently verified recipient. Read status after a lost
initialization response; do not blindly initialize again or cancel another
custodian's attempt. The fingerprint comparison is case-insensitive hexadecimal.

Each custodian submits their own share through standard input. Replace `NONCE`
with the verified nonce. Use a distinct output file for each submission, preserving
the final encrypted response: replace `CUSTODIAN` below with the submitter's name.
An expired session can be replaced through another
independent login without starting a new ceremony.

```sh
bao operator generate-root -format=json -nonce=NONCE - \
  < /secure/custodian-share > /secure/emergency/incident-001/submission-CUSTODIAN.json
```

Before the quorum, `complete` is false and there is no token. Repeated shares do
not advance the quorum. At completion, verify the nonce and recipient again and
preserve the response's `encoded_token`. This field is a base64-encoded PGP
message, not the usable root token. Decode the final submitter's response on the
protected workstation, replacing `FINAL_CUSTODIAN` with their name:

```sh
python3 -c 'import base64, json, sys; value=json.load(sys.stdin); assert value["complete"]; sys.stdout.buffer.write(base64.b64decode(value["encoded_token"], validate=True))' \
  < /secure/emergency/incident-001/submission-FINAL_CUSTODIAN.json \
  > /secure/emergency/incident-001/root-token.pgp
gpg --output /secure/emergency/incident-001/root-token \
  --decrypt /secure/emergency/incident-001/root-token.pgp
```

The generated root token has unrestricted authority and no automatic expiration.
Use it only for the recorded recovery work, then revoke it and the emergency
session. Run the revocation even when recovery work fails; retain independent
access for further investigation.

```sh
export BAO_TOKEN="$(cat -- /secure/emergency/incident-001/root-token)"
# Perform and verify the recorded administrative recovery work.
bao token revoke -self
bao token lookup -field=ttl
```

The final lookup must fail with HTTP 403. A TLS or connectivity failure does not
prove revocation. Then revoke the emergency session with its token and unset the
environment variable. Review audit events and remove temporary plaintext under
the custody procedure; deleting a file is not secure erasure.

```sh
export BAO_TOKEN="$(cat -- /secure/emergency/session-token)"
bao token revoke -self
unset BAO_TOKEN
```

**A lost final response is an incident, not a retry signal.** OpenBao clears a
completed attempt; status cannot retrieve its encrypted token afterward.
`generate-root -cancel` only cancels an unfinished ceremony and does not revoke
an already-issued root token. Preserve the nonce and audit history. Reconcile
issued-token accessors through independently authorized administration before
retiring unknown authority. Do not infer that an absent attempt means no token
was created. This procedure has no automatic recovery of that lost response.

## Rotate, withdraw and restore

After a drill or emergency, an authorized administrator can rotate the password
using `bao write auth/chipload-emergency/users/custodian/password
password=@/secure/emergency/replacement-password`. Persist and protect the new
random password first. Verify new login succeeds and the old password receives
HTTP 403 before updating the independently retained custody copy. Changing a
password does not revoke existing tokens. Revoke known sessions or allow their
five-minute maximum lifetime to pass; investigate unknown sessions during an
incident.

To withdraw the whole emergency path, disable its dedicated mount with
`bao auth disable chipload-emergency`. Verify existing sessions and new logins
fail before removing its policy. Establish and test another emergency path before
withdrawing this one. These changes require full administrative authority; the
emergency login alone cannot perform them.

The encrypted [OpenBao recovery archive](SECRETS.md#encrypted-offline-backup-and-restore)
preserves the auth mount, password hash, policy and token revocations in the Raft
store. It does not contain the independently held password or private PGP keys.
Restoring an earlier checkpoint can restore an earlier password or token
authority; reconcile changes since the backup before exposing the service.

`test_secrets_emergency_live.py` runs the actual CLI against the private TLS
service with no Keycloak instance. It checks narrow ACL denials, the disabled
legacy endpoint, root retirement, incorrect nonce and duplicate-share rejection,
encrypted quorum completion, cancellation semantics, password rotation, audit
redaction, and fresh login/root generation after deleting and restoring the
original service state. It revokes all generated root tokens. This qualifies the
software procedure on one host; independent human custody, off-host recovery and
an organization's incident response still require a deployment drill.
