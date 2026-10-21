# Obtain and renew deployment certificates

`scripts/tls-acme` runs a pinned lego client in a separate container. It obtains
an ECDSA certificate covering the API and identity hostnames, retains the ACME
account, renews when due, and can deliver renewed material through
[`tls-certificates`](TLS.md#install-and-monitor-certificates). The gateway and
Keycloak use the same two-name certificate and private key. REST, MCP, shop
Python, and application containers cannot access the issuer account.

This implementation supports HTTP-01 and an HTTPS ACME directory. DNS challenges,
wildcards, external-account binding, account-key rollover, revocation automation,
and automatic CA trust changes are not implemented. The local development CA
created by `tls-stack init --local` is a separate workflow.

## Prepare the host and provider

Use the non-root deployment owner, Python 3.12+, the locked server environment,
local Docker access, and the built images described in [TLS setup](TLS.md).
Public challenge binding requires Docker Engine 28 or newer. Pull the issuer:

```sh
uv sync --locked --extra server
./scripts/acme-images
```

Select your CA's HTTPS directory, review its terms, and obtain its issued-server
root CA bundle through a trusted channel. `--ca-bundle` is the explicit trust
used to validate issued certificates and by the deployment; it accepts at most
32 certificates and 1 MiB. `--directory-ca` instead verifies the ACME server's
own HTTPS connection. Without that option, initialization copies the host's
current system roots for that connection. Neither trust set updates itself.

Test public DNS and routing with the provider's staging environment first, using
a separate issuer state directory and separate test trust. For example,
[Let's Encrypt staging](https://letsencrypt.org/docs/staging-environment/) has
its own directory, account scope and untrusted test roots. Never install staging
roots into production trust.

Both hostnames must route inbound HTTP port 80 to this host's challenge listener.
The helper defaults to `127.0.0.1:80`; explicitly select a public IPv4 interface
or `0.0.0.0` for direct validation. Another HTTP server cannot occupy that same
address and port. With a frontend, forward `/.well-known/acme-challenge/` to the
configured listener. `--http-port` changes the host port, not the CA's public
validation port. See [HTTP-01 requirements](https://letsencrypt.org/docs/challenge-types/).

The issuer uses IPv4. Publish working A records, and only publish AAAA records
when an IPv6 frontend also delivers challenges correctly. An incorrect AAAA
record can cause validation failure even when IPv4 works; see the provider's
[IPv6 behavior](https://letsencrypt.org/docs/ipv6-support/). Permit the client
container's DNS and outbound HTTPS. The HTTP listener runs only during the ACME
client invocation; it does not expose shop discovery or administration.

## Issue and bind

Set `CHIPLOAD_ACME_DIRECTORY` to the selected provider directory and
`CHIPLOAD_ISSUED_CA_BUNDLE` to its reviewed root bundle. Replace the example
hostnames and email with your own. Initialization writes private state without
contacting the provider; `run` accepts the configured terms, creates an account
if needed, and requests certificates.

```sh
uv run --extra server python scripts/tls-acme --state .local/acme init \
  --api-host shop.example.com --identity-host identity.example.com \
  --server "$CHIPLOAD_ACME_DIRECTORY" --email operator@example.com \
  --accept-terms --ca-bundle "$CHIPLOAD_ISSUED_CA_BUNDLE" \
  --listen-address 0.0.0.0
uv run --extra server python scripts/tls-acme --state .local/acme run
```

`current` points to a validated, private generation under `bundles/`. The output
contains its absolute path and public certificate metadata. Use it to initialize
a fresh deployment, choosing the public HTTPS address explicitly:

```sh
uv run --extra server python scripts/tls-stack --state .local/site init \
  --name chipload-site --shop my-shop \
  --api-host shop.example.com --identity-host identity.example.com \
  --port 443 --admin-port 8444 --listen-address 0.0.0.0 \
  --certificates .local/acme/current/certificates \
  --ca-bundle .local/acme/current/identity-ca.pem
uv run --extra server python scripts/tls-stack --state .local/site up
uv run --extra server python scripts/tls-acme --state .local/acme bind \
  --deployment .local/site
```

For an existing supplied-certificate deployment, bind it after checking that
both names and the exact CA bundle match. Binding records the deployment's
configuration and current material. It authorizes subsequent `run` invocations
to install the issuer's current bundle, which may differ from a manually issued
certificate already installed there. Review that bundle before binding.

`run` checks renewal and, when bound, installs changed material. The default
`--renew-days 0` uses lego's certificate-lifetime calculation and ACME Renewal
Information. A positive value selects the remaining-day threshold; do not use
a threshold longer than the certificate's lifetime in production. Identical
material causes no deployment restart. Installation of changed material briefly
recreates the gateway, Keycloak and API, verifies the exact served fingerprints,
and preserves identities and grants. A deployment already stopped stays stopped.

## Schedule and monitor

After a successful bound run, generate reviewable user-systemd units:

```sh
uv run --extra server python scripts/tls-acme --state .local/acme run
uv run --extra server python scripts/tls-acme --state .local/acme timer
systemd-analyze --user verify .local/acme/systemd/*.service .local/acme/systemd/*.timer
systemctl --user link "$PWD"/.local/acme/systemd/*.service "$PWD"/.local/acme/systemd/*.timer
systemctl --user daemon-reload
systemctl --user start "$(basename .local/acme/systemd/*.service)"
systemctl --user enable --now "$(basename .local/acme/systemd/*.timer)"
systemctl --user list-timers 'chipload-acme-*'
```

The generator does not enable anything and refuses to overwrite existing units.
The timer checks at 00:00 and 12:00 UTC with up to thirty minutes of random delay;
persistent calendar scheduling catches up after downtime. Review a shorter
interval if the provider's certificate lifetime requires it. The service uses
the current checkout and virtualenv by absolute path, a twenty-minute timeout,
private umask, no-new-privileges, memory/process limits, and disabled core dumps.
Keep those paths stable. Store issuer and deployment state outside `/tmp` because
the unit also requests a private temporary directory.

Enable user lingering through your host's administration if renewal must run
without an interactive login, and verify the timer after reboot. The scheduling
semantics follow [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html).
Filesystem restrictions in user units depend on the host and may not be enforced;
this was observed on the integration host. The host runner remains a trusted
operator process with Docker authority. Container restrictions described below
provide the client's isolation; user-systemd settings do not remove Docker's
host authority. See [systemd execution restrictions](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).

Monitor both the service result and expiry, with alert delivery configured in
your own monitoring system:

```sh
journalctl --user --unit 'chipload-acme-*' --since today
uv run --extra server python scripts/tls-certificates --state .local/site status --warn-days 14
```

Certificate status returns 0 for valid material outside the warning window,
2 for approaching expiry, and 1 for invalid material or pending installation.
It is an offline check; also monitor the actual HTTPS endpoints. `last-run.json`
records completed client outcomes. A preflight failure can leave that previous
receipt unchanged, so it cannot replace service-failure monitoring. Provider
rate-limit handling and site-specific failure alerting need operational checks.

## Interruption and operator changes

Retry `run` with the same issuer state. An interrupted run retains its container
identity and delivery intent. A completed issuance is reused while installation
resumes; if the pending certificate has expired, renewal can supply a valid
replacement. A client failure retains previously exported and deployed material.
An unavailable Docker daemon or an unconfirmed running client is not success.

The issuer refuses automatic delivery if configuration, trust, or certificate
bytes changed outside its recorded operation, or another recovery/credential
operation is pending. It does not adopt another installation's journal. Finish
that operation first. For an intentional manual takeover:

1. Stop and disable this issuer's timer; let an active service finish or inspect
   its recorded container before intervening.
2. Run `tls-acme --state .local/acme unbind` through the same Python environment.
   This durably disables delivery and preserves deployment files and any pending
   `tls-certificates` journal. It does not stop the timer or revoke certificates.
3. Complete deployment recovery with `tls-certificates resume` or a validated
   `replace-pending`, as described in [certificate operations](TLS.md#install-and-monitor-certificates).
4. Run the unbound issuer to finish any retained issuance, review its current
   bundle, and explicitly bind again only when it should control this deployment.
   Then run once successfully before reenabling the timer.

An expired historical deployment can be repaired after offline restore. Its
replacement deployment path must be explicitly bound; copying a previous binding
does not authorize installation into another deployment.

## Custody and boundaries

The named issuer container is retained between invocations and starts by immutable
ID. Preserve it across ordinary renewals; exclude it from stopped-container
pruning. The helper checks its pinned image, command, ownership labels, mounts,
network, port and resource/security settings before starting it. Account keys
and issued state live under the owner's private `client/` directory. Missing or
changed established account keys cause refusal instead of silent re-registration.

The client runs as the deployment owner's non-root UID/GID, with a read-only
image, all capabilities dropped, no-new-privileges, 256 MiB memory/swap limit,
one CPU, 64 processes, bounded logs and an 8 MiB temporary filesystem. It has
only its own writable client storage and read-only configuration/directory trust;
it has no Docker socket, shop databases or deployment credentials. Proxy
environment variables are explicitly empty. TLS verification remains enabled.
Only the separate host helper has deployment and Docker authority.

Issuer state is **not included** in the [TLS recovery archive](RECOVERY.md).
Use the separate [issuer backup and restore helper](ACME_RECOVERY.md) for encrypted
account custody, and retain verified archives and their key off-host. It preserves
account registration and keys, restores into fresh private state, and requires
an explicit deployment binding before delivery. Do not reinitialize over existing
state or selectively restore an account key. Off-host recovery qualification,
account rollover, image/trust migration and generation retention remain work. An established
issuer is pinned to its configuration and image; changing checkout pins alone
does not upgrade it safely. TLS recovery still preserves the deployed leaf keys
and certificate receipt, allowing manual repair if issuer recovery is unavailable.

## Verify

```sh
./scripts/acme-images --tests
CHIPLOAD_ACME_TESTS=1 uv run --extra server python -m unittest discover -s tests -p 'test_acme_live.py' -v
```

The live suite needs the built TLS/runtime/worker images and a running user
systemd manager. It uses the pinned Pebble test CA with actual domain validation
through a published HTTP port, verified directory TLS and private test trust.
It exercises issuance, renewal, no-op checks, CA failure, account loss, container
policy drift, installer process death, resumed delivery, unchanged real identity
grants, and an accelerated systemd timer. Only its own units, containers, networks
and volumes are removed. It makes no public CA requests and does not qualify a
real site's DNS, firewall, provider account or recovery custody.
