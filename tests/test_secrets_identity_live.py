"""Real browser device login, subject-scoped operators, key rollover and recovery."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urljoin

import httpx
import jwt
from secrets_support import ROOT, archive, fixture_args, script

identity = script.load("identity_live", "scripts/secrets-identity")
recovery = script.load("identity_bao_recovery", "scripts/secrets-recovery")


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""), "fields": {}, "id": attrs.get("id")}
            self.forms.append(self.current)
        if tag in {"input", "button"} and self.current is not None and attrs.get("name"):
            if attrs.get("name") not in {"cancel", "deny"}:
                self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_SECRETS_TESTS") == "1", "requires TLS images, OpenBao and GnuPG"
)
class IdentityLiveTests(unittest.TestCase):
    def test_device_login_scoped_grants_root_retirement_keys_and_recovery(self):
        with tempfile.TemporaryDirectory(prefix="chipload-identity-test-") as temporary:
            root = Path(temporary)
            tls_state = root / "identity"
            prefix = "chipload-identity-test-" + uuid.uuid4().hex[:10]
            network = prefix + "-adapter"
            listeners = [socket.socket(), socket.socket()]
            for listener in listeners:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in listeners)
            for listener in listeners:
                listener.close()

            def run(*cmd):
                return subprocess.check_output(cmd, stderr=subprocess.PIPE, text=True, timeout=360)

            def tls_cli(action):
                return run(
                    sys.executable,
                    str(ROOT / "scripts/tls-stack"),
                    "--state",
                    str(tls_state),
                    action,
                )

            run(
                sys.executable,
                str(ROOT / "scripts/tls-stack"),
                "--state",
                str(tls_state),
                "init",
                "--local",
                "--name",
                prefix,
                "--port",
                str(port),
                "--admin-port",
                str(admin_port),
                "--shop",
                "example",
            )
            gpg, setup = fixture_args(root, name=prefix + "-bao", network=network)
            state = setup.state

            def service(action, **kwargs):
                return script.operate(SimpleNamespace(state=state, action=action, **kwargs))

            run("docker", "network", "create", "--internal", network)
            try:
                tls_cli("up")
                script.configure(setup)

                service("up")
                service("initialize")
                bootstrap = script.read_json(state / "bootstrap.json")

                def decrypt(packet):
                    return (
                        subprocess.check_output(
                            [*gpg, "--decrypt"],
                            input=base64.b64decode(packet),
                            stderr=subprocess.DEVNULL,
                        )
                        .decode()
                        .strip()
                    )

                shares = []
                for index, packet in enumerate(bootstrap["keys_base64"]):
                    path = root / f"share-{index}"
                    archive.write(path, decrypt(packet))
                    shares.append(path)
                service("unseal", share_file=shares[0])
                service("unseal", share_file=shares[1])
                root_token = root / "bootstrap-token"
                archive.write(root_token, decrypt(bootstrap["root_token"]))
                keycloak_token = root / "keycloak-token"
                values = script.read_json(tls_state / "deployment.json")

                def provider():
                    bearer = identity.credentials.operator_token(
                        tls_state, values, "admin", tls_state / "secrets/admin-password"
                    )
                    if keycloak_token.exists():
                        keycloak_token.write_text(bearer)
                    else:
                        archive.write(keycloak_token, bearer)
                    return identity.Provider(tls_state, bearer)

                password = secrets.token_urlsafe(24)
                users = {}
                otp_secrets = {}
                for username in ("operator", "rotator", "outsider"):
                    subject = str(uuid.uuid4())
                    otp_secrets[username] = secrets.token_bytes(20)
                    provider().admin(
                        "/users",
                        method="POST",
                        data={
                            "id": subject,
                            "username": username,
                            "enabled": True,
                            "firstName": username,
                            "lastName": "Fixture",
                            "email": username + "@example.invalid",
                            "emailVerified": True,
                            "credentials": [
                                {"type": "password", "value": password, "temporary": False},
                                {
                                    "type": "otp", "userLabel": "Fixture authenticator",
                                    "secretData": json.dumps({
                                        "value": base64.b32encode(otp_secrets[username]).decode()
                                    }),
                                    "credentialData": json.dumps({
                                        "subType": "totp", "digits": 6, "period": 30,
                                        "counter": 0, "algorithm": "HmacSHA1",
                                        "secretEncoding": "BASE32",
                                    }),
                                },
                            ],
                        },
                        expected=(201,),
                    )
                    created = provider().admin("/users?username=" + username + "&exact=true")[1]
                    self.assertEqual(len(created), 1)
                    users[username] = created[0]["id"]
                args = SimpleNamespace(
                    state=state,
                    identity_state=tls_state,
                    operator_token=root_token,
                    keycloak_token=keycloak_token,
                    action="grant",
                    name="admin",
                    subject=users["operator"],
                    administrator=True,
                    workload=[],
                )
                original_admin = identity.Provider.admin

                def lost_client(obj, path, **kwargs):
                    result = original_admin(obj, path, **kwargs)
                    if path == "/clients" and kwargs.get("method") == "POST":
                        raise OSError("lost committed client registration")
                    return result

                provider()
                with patch.object(identity.Provider, "admin", new=lost_client):
                    with self.assertRaises(OSError):
                        identity.operate(args)
                self.assertTrue((state / identity.MARKER).exists())
                self.assertTrue((tls_state / identity.MARKER).exists())
                pending_key = root / "pending-backup-key"
                archive.generate_key(pending_key)
                with self.assertRaisesRegex(ValueError, "identity change"):
                    recovery.backup(
                        SimpleNamespace(
                            state=state, key=pending_key, archive=root / "pending-bao.gpg"
                        )
                    )
                tls_recovery = script.load("operator_tls_recovery", "scripts/tls-recovery")
                with self.assertRaisesRegex(ValueError, "identity change"):
                    tls_recovery.backup(tls_state, root / "pending-tls.gpg", pending_key, 2**30)
                with self.assertRaisesRegex(ValueError, "identity change"):
                    identity.credentials.rotate(
                        tls_state, "agent", password_file=tls_state / "secrets/admin-password"
                    )
                args.action = "resume"
                provider()
                client_identity = identity.pending(state, tls_state)["identity"]
                actual_client = provider().admin("/clients/" + client_identity["client_uuid"])[1]
                self.assertTrue(
                    identity.same_client(actual_client, identity.client_config(client_identity)),
                    repr(actual_client["protocolMappers"]),
                )
                observed_call = identity.workload.API.call

                def check_configuration(obj, method, path, *values, **kwargs):
                    result = observed_call(obj, method, path, *values, **kwargs)
                    if (
                        method == "GET"
                        and path == f"auth/{identity.MOUNT}/config"
                        and result[0] == 200
                    ):
                        for field, expected in identity.jwt_config(client_identity).items():
                            self.assertEqual(result[1]["data"].get(field), expected, field)
                    return result

                with patch.object(identity.workload.API, "call", new=check_configuration):
                    identity.operate(args)
                configured = script.read_json(state / identity.RECEIPT)
                self.assertEqual(
                    len(provider().admin("/clients?clientId=" + configured["client_id"])[1]), 1
                )
                browser_context = ssl.create_default_context(cafile=tls_state / "identity-ca.pem")
                otp_counters = {}

                def otp(username):
                    # Keycloak rejects replay of a TOTP within its valid window.
                    # Wait for a new authenticator code between separate logins.
                    counter = int(time.time()) // 30
                    if counter <= otp_counters.get(username, -1):
                        time.sleep((counter + 1) * 30 - time.time() + 0.1)
                        counter = int(time.time()) // 30
                    otp_counters[username] = counter
                    digest = hmac.digest(
                        otp_secrets[username], counter.to_bytes(8, "big"), "sha1"
                    )
                    offset = digest[-1] & 15
                    return f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1000000:06d}"

                rejected_otp = set()

                def approve(username):
                    def browser(message, **_):
                        match = re.fullmatch(
                            r"Open (.+) and confirm code ([A-Za-z0-9-]+)\..*", message
                        )
                        self.assertIsNotNone(match)
                        url, code = match.groups()
                        with httpx.Client(
                            verify=browser_context,
                            trust_env=False,
                            follow_redirects=True,
                            timeout=30,
                        ) as client:
                            page = client.get(url)
                            for _ in range(8):
                                forms = Forms(page.text).forms
                                if not forms:
                                    self.assertTrue("success" in page.text.lower(),
                                        "device login did not finish; feedback=" + repr(re.findall(
                                            r'<p class="instruction">([^<]*)', page.text
                                        )))
                                    return
                                form = next(
                                    (
                                        f
                                        for f in forms
                                        if any(
                                            k in f["fields"]
                                            for k in ("username", "otp", "device_user_code", "accept")
                                        )
                                    ),
                                    forms[0],
                                )
                                data = form["fields"]
                                if "username" in data:
                                    data.update(username=username, password=password)
                                elif "otp" in data:
                                    if username == "outsider" and username not in rejected_otp:
                                        wrong = client.post(
                                            urljoin(str(page.url), form["action"]),
                                            data={**data, "otp": "invalid-code"},
                                        )
                                        self.assertEqual(wrong.status_code, 200)
                                        self.assertTrue(any(
                                            "otp" in item["fields"] for item in Forms(wrong.text).forms
                                        ), "an invalid OTP must leave authentication incomplete")
                                        rejected_otp.add(username)
                                        page = wrong
                                        continue
                                    data["otp"] = otp(username)
                                elif "device_user_code" in data:
                                    data["device_user_code"] = code
                                elif "accept" in data:
                                    data["accept"] = "Yes"
                                else:
                                    self.fail("unknown device-login form fields: " + ",".join(data))
                                page = client.post(
                                    urljoin(str(page.url), form["action"]), data=data
                                )
                                feedback = re.findall(
                                    r'<[^>]*class="[^"]*kc-feedback-text[^"]*"[^>]*>([^<]*)',
                                    page.text,
                                )
                                self.assertEqual(
                                    page.status_code,
                                    200,
                                    f"HTTP {page.status_code}; form fields {sorted(data)}; feedback {feedback}; "
                                    f"next forms {[sorted(candidate['fields']) for candidate in Forms(page.text).forms]}; "
                                    f"title {re.findall(r'<title>([^<]*)', page.text)}; "
                                    f"error codes {re.findall(r'\"error\"\s*:\s*\"([a-z_]+)\"', page.text)}",
                                )
                            self.fail("device login exceeded its redirect/form limit")

                    return browser

                raw_jwts = {}
                original_device = identity.device_login

                def get_jwt(username, *, mfa=True):
                    polls = 0

                    def approved(_):
                        nonlocal polls
                        polls += 1
                        self.assertLessEqual(
                            polls, 3, "browser approval did not finish device login"
                        )

                    request = identity.device_request

                    def check_token_response(*args, **kwargs):
                        status, result = request(*args, **kwargs)
                        if configured["version"] == 2 and status == 200 and "access_token" in result:
                            self.assertTrue(not result.get("refresh_token"),
                                            "the operator client must not issue refresh tokens")
                        return status, result

                    with patch.object(identity, "device_request", side_effect=check_token_response):
                        value = original_device(
                            identity.Provider(tls_state),
                            configured,
                            announce=approve(username),
                            sleep=approved,
                        )
                    raw_jwts[username] = value
                    claims = jwt.decode(value, options={"verify_signature": False})
                    if mfa:
                        self.assertEqual(set(claims.get("amr", [])), {"pwd", "otp"})
                    return value

                def login(username, grant, filename):
                    target = root / filename
                    login_args = SimpleNamespace(
                        state=state,
                        identity_state=tls_state,
                        action="login",
                        name=grant,
                        output=target,
                        jwt_file=None,
                    )
                    with patch.object(
                        identity, "device_login", side_effect=lambda *_: get_jwt(username)
                    ):
                        identity.operate(login_args)
                    return target

                admin_token = login("operator", "admin", "admin-token")
                admin = identity.workload.API(state, admin_token)
                # A real provider-signed password-only token must fail at OpenBao,
                # including when the caller bypasses the local login helper.
                flow, executions = identity.operator_mfa.specification(configured["mfa_flow"])
                execution_path = "/authentication/flows/" + flow["alias"] + "/executions"
                otp_execution = executions[1][0]["id"]
                provider().admin(execution_path, method="PUT", data={
                    "id": otp_execution, "requirement": "DISABLED"
                }, expected=(204,))
                try:
                    with self.assertRaisesRegex(ValueError, "differs"):
                        identity.operator_mfa.ensure(provider(), configured["mfa_flow"])
                    password_only = get_jwt("operator", mfa=False)
                    self.assertEqual(
                        jwt.decode(password_only, options={"verify_signature": False})["amr"], ["pwd"]
                    )
                finally:
                    provider().admin(execution_path, method="PUT", data={
                        "id": otp_execution, "requirement": "REQUIRED"
                    }, expected=(204,))
                self.assertEqual(admin.call(
                    "POST", f"auth/{identity.MOUNT}/login", {"role": "admin", "jwt": password_only},
                    token=None, statuses=(400,),
                )[0], 400)
                # Recreate the previous released identity contract as an upgrade
                # fixture. The production command offers no MFA downgrade.
                legacy = {key: value for key, value in configured.items() if key != "mfa_flow"}
                legacy["version"] = 1
                legacy_client = identity.client_config(legacy)
                # A version-1 client could use an operator-selected password-only
                # browser flow. Keep that explicit instead of inheriting the realm flow.
                legacy_client["authenticationFlowBindingOverrides"] = {"browser": flow["id"]}
                provider().admin(execution_path, method="PUT", data={
                    "id": otp_execution, "requirement": "DISABLED"
                }, expected=(204,))
                provider().admin("/clients/" + legacy["client_uuid"], method="PUT",
                                 data=legacy_client, expected=(204,))
                receipt = state / "operators/admin.json"
                preserved_grant = receipt.read_bytes()
                grant = script.read_json(receipt)
                admin.call("POST", f"auth/{identity.MOUNT}/role/admin", identity.role_config(legacy, grant))
                archive.replace_json(state / identity.RECEIPT, legacy)
                configured = legacy
                legacy_jwt = get_jwt("operator", mfa=False)
                self.assertNotIn("amr", jwt.decode(legacy_jwt, options={"verify_signature": False}))
                issued = admin.call("POST", f"auth/{identity.MOUNT}/login",
                                   {"role": "admin", "jwt": legacy_jwt}, token=None)[1]
                admin.call("POST", "auth/token/revoke-self", token=issued["auth"]["client_token"])
                args.action = "enable-mfa"
                provider()
                original_call = identity.workload.API.call

                def lost_mfa_role(obj, method, path, *values, **kwargs):
                    result = original_call(obj, method, path, *values, **kwargs)
                    if method == "POST" and path == f"auth/{identity.MOUNT}/role/admin":
                        raise OSError("lost committed MFA role update")
                    return result

                with patch.object(identity.workload.API, "call", new=lost_mfa_role):
                    with self.assertRaises(OSError):
                        identity.operate(args)
                configured = identity.pending(state, tls_state)["identity"]
                self.assertEqual(admin.call("POST", f"auth/{identity.MOUNT}/login",
                    {"role": "admin", "jwt": legacy_jwt}, token=None, statuses=(400,))[0], 400)
                # A completed administrator can reauthenticate with MFA and
                # resume the interrupted upgrade without using the root token.
                args.operator_token = login("operator", "admin", "mfa-resume-token")
                args.action = "resume"
                provider()
                self.assertEqual(identity.operate(args)["status"], "mfa-enabled")
                self.assertEqual(receipt.read_bytes(), preserved_grant)
                self.assertEqual(configured["client_uuid"], legacy["client_uuid"])
                configured = script.read_json(state / identity.RECEIPT)
                admin_token = login("operator", "admin", "mfa-admin-token")
                admin = identity.workload.API(state, admin_token)
                root_value = root_token.read_text().strip()
                admin.call("POST", "auth/token/revoke-self", token=root_value)
                self.assertEqual(
                    admin.call("GET", "auth/token/lookup-self", token=root_value, statuses=(403,))[
                        0
                    ],
                    403,
                )
                self.assertNotIn(
                    "root", admin.call("GET", "auth/token/lookup-self")[1]["data"]["policies"]
                )
                # The shop's service-account token cannot acquire operator access.
                shop_jwt = identity.credentials.expect(
                    *identity.credentials.agent_token(
                        tls_state, values, (tls_state / "secrets/agent-secret").read_text()
                    ),
                    200,
                )["access_token"]
                for token in (shop_jwt, get_jwt("outsider")):
                    self.assertEqual(
                        admin.call(
                            "POST",
                            f"auth/{identity.MOUNT}/login",
                            {"role": "admin", "jwt": token},
                            token=None,
                            statuses=(400, 403),
                        )[0],
                        400,
                    )
                # Create a real workload and give a second subject only rotation authority.
                credentials_dir = root / "workload"
                credentials_dir.mkdir(mode=0o700)
                archive.write(
                    credentials_dir / "bao-ca.pem", (state / "certificates/ca.pem").read_bytes()
                )
                adapter = root / "adapter.json"
                archive.write(
                    adapter,
                    json.dumps(
                        {
                            "id": "evidence",
                            "receiver_id": "evidence",
                            "origin": "https://receiver:8443",
                            "contracts": [
                                {
                                    "shop_id": "example",
                                    "operation_id": "review",
                                    "command": "review",
                                    "effect": "read",
                                }
                            ],
                            "secrets": {
                                "origin": "https://bao:8200",
                                "ca_file": "bao-ca.pem",
                                "kv_mount": "chipload",
                                "secret_path": "shops/example/evidence",
                                "policy": "evidence",
                            },
                        }
                    ),
                )
                receiver = root / "receiver-token"
                archive.write(receiver, secrets.token_urlsafe(32))
                work_args = SimpleNamespace(
                    state=state,
                    operator_token=admin_token,
                    action="provision",
                    adapter=adapter,
                    credentials=credentials_dir,
                    receiver_secret=receiver,
                    secret_id_ttl=3600,
                )
                identity.workload.operate(work_args)
                (
                    args.action,
                    args.name,
                    args.subject,
                    args.administrator,
                    args.workload,
                    args.operator_token,
                ) = "grant", "rotation", users["rotator"], False, ["evidence"], admin_token
                provider()
                identity.operate(args)
                rotator_token = login("rotator", "rotation", "rotator-token")
                work_args.action, work_args.workload, work_args.operator_token = (
                    "rotate",
                    "evidence",
                    rotator_token,
                )
                self.assertEqual(identity.workload.operate(work_args)["generation"], 2)
                rotator = identity.workload.API(state, rotator_token)
                for method, path, data in (
                    ("GET", "sys/mounts", None),
                    ("GET", "chipload/data/shops/example/evidence", None),
                    ("POST", "auth/approle/role/another/custom-secret-id", {"secret_id": "denied"}),
                    (
                        "PUT",
                        "sys/policies/acl/evidence",
                        {"policy": 'path "*" { capabilities = ["read"] }'},
                    ),
                ):
                    self.assertEqual(rotator.call(method, path, data, statuses=(403,))[0], 403)
                self.assertEqual(
                    admin.call(
                        "POST",
                        f"auth/{identity.MOUNT}/login",
                        {"role": "admin", "jwt": raw_jwts["rotator"]},
                        token=None,
                        statuses=(400, 403),
                    )[0],
                    400,
                )
                # Revoke before token expiry; the existing token loses policy access.
                revoked_at = time.monotonic()
                args.action = "revoke"
                provider()
                original_call = identity.workload.API.call

                def lost_revoke(obj, method, path, *values, **kwargs):
                    result = original_call(obj, method, path, *values, **kwargs)
                    if method == "PUT" and path == "sys/policies/acl/chipload-operator-rotation":
                        raise OSError("lost committed operator revocation")
                    return result

                with patch.object(identity.workload.API, "call", new=lost_revoke):
                    with self.assertRaises(OSError):
                        identity.operate(args)
                # Completed unaffected grants can log in again to resume with a fresh token.
                args.operator_token = login("operator", "admin", "resume-admin")
                args.action = "resume"
                provider()
                identity.operate(args)
                self.assertLess(time.monotonic() - revoked_at, 60)
                self.assertEqual(
                    rotator.call("GET", "auth/approle/role/evidence", statuses=(403,))[0], 403
                )
                # Refresh admin identity before changing the realm's actual signing key.
                admin_token = login("operator", "admin", "fresh-admin")
                old_jwt = raw_jwts["operator"]
                identity.credentials.rotate(
                    tls_state,
                    "signing",
                    password_file=tls_state / "secrets/admin-password",
                    retain_days=1,
                )
                new_jwt = get_jwt("operator")
                with self.assertRaisesRegex(ValueError, "not synchronized"):
                    identity.check_jwt(
                        configured, script.read_json(state / "operators/admin.json"), new_jwt
                    )
                args.action, args.operator_token = "sync-keys", admin_token
                provider()
                identity.operate(args)
                configured = script.read_json(state / identity.RECEIPT)
                new_admin = login("operator", "admin", "new-key-admin")
                signing = identity.credentials.signing
                with patch.object(signing, "now", return_value=int(time.time()) + 2 * 86400):
                    identity.credentials.rotate(
                        tls_state,
                        "signing-retire",
                        password_file=tls_state / "secrets/admin-password",
                    )
                args.operator_token = new_admin
                provider()
                identity.operate(args)
                configured = script.read_json(state / identity.RECEIPT)
                self.assertGreater(
                    jwt.decode(old_jwt, options={"verify_signature": False})["exp"] - time.time(), 0
                )
                admin = identity.workload.API(state, new_admin)
                self.assertEqual(
                    admin.call(
                        "POST",
                        f"auth/{identity.MOUNT}/login",
                        {"role": "admin", "jwt": old_jwt},
                        token=None,
                        statuses=(400, 403),
                    )[0],
                    400,
                )
                # Operator receipts and trust survive a fresh OpenBao store restore.
                key, output = root / "recovery-key", root / "backup.gpg"
                archive.generate_key(key)
                backup = SimpleNamespace(state=state, key=key, archive=output)
                recovery.backup(backup)
                before = hashlib.sha256((state / identity.RECEIPT).read_bytes()).hexdigest()
                shutil.rmtree(state)
                state = root / "restored"
                backup.state = state
                recovery.restore(backup)
                service("up")
                service("unseal", share_file=shares[1])
                service("unseal", share_file=shares[2])
                self.assertEqual(
                    hashlib.sha256((state / identity.RECEIPT).read_bytes()).hexdigest(), before
                )
                restored_admin = login("operator", "admin", "restored-admin")
                restored = identity.workload.API(state, restored_admin)
                self.assertEqual(restored.call("GET", "sys/auth")[0], 200)
                self.assertEqual(
                    script.read_json(state / "operators/rotation.json")["status"], "revoked"
                )
                self.assertFalse((state / identity.MARKER).exists())
                self.assertFalse((tls_state / identity.MARKER).exists())
            finally:
                if state.exists():
                    service("down")
                run("docker", "network", "rm", network)
                tls_cli("down")
                volumes = run(
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + prefix,
                ).split()
                if volumes:
                    run("docker", "volume", "rm", *volumes)
                subprocess.run(
                    ["gpgconf", "--homedir", str(root / "gnupg"), "--kill", "gpg-agent"],
                    capture_output=True,
                )
