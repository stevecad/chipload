"""Disposable three-instance federation drill with real Keycloak, OpenBao and workers."""

import asyncio
import base64
import copy
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx2
from credentials_support import credentials
from mcp import Client as MCPClient
from mcp.client.streamable_http import streamable_http_client
from secrets_support import fixture_args
from secrets_support import script as bao_stack
from tls_support import ROOT, stack

from chipload.access import Grant, Principal
from chipload.auth_settings import ServerSettings
from chipload.client import Client, ServiceError, TransportError
from chipload.cloud_client import CloudClient
from chipload.connector import CloudLink, ConnectorSettings, DispatchLink
from chipload.connector_auth import WorkloadIdentity
from chipload.directory_model import CloudSettings, OwnerBinding, SupplierSearch
from chipload.federation_wire import EgressPolicy, PrivateRoute
from chipload.model import GeoPoint
from chipload.network_model import DirectRead
from chipload.peer import PeerClient
from chipload.peer_model import PeerCatalog, PeerRequest
from chipload.remote_adapter import OpenBao
from examples.federation_shops import project, query, term


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def run(*arguments):
    completed = subprocess.run([str(arg) for arg in arguments], capture_output=True, text=True, timeout=240)
    if completed.returncode:
        # Commands contain paths and deployment names only; secret requests use private files/IPC.
        raise AssertionError(f"command failed ({arguments[0]}): {completed.stderr[-4000:]}")
    return completed.stdout.strip()


def wait_for(read, accept, *, seconds=120):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = read()
        except (ServiceError, TransportError):
            if time.monotonic() >= deadline:
                raise
        else:
            if accept(value):
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for federation state: {value}")
        time.sleep(1)


def private_json(path, value):
    temporary = path.with_suffix(".staged")
    stack.write(temporary, value.model_dump_json(indent=2) if hasattr(value, "model_dump_json") else json.dumps(value))
    temporary.replace(path)


# The same code runs through REST and the existing four-tool MCP interface.
# Each remote read has its own absolute deadline; source code receives no token or egress policy.
SUPPLIER_PROGRAM = '''
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import time
import uuid
from chipload.network_model import DirectoryRead, DirectRead, RelayRead
from chipload.peer_model import PeerListing, PeerRequest
from chipload.directory_model import SupplierSearch
from chipload.listings import ListingQuery
shop = client.shop('mill')
def read(intent):
    deadline = getattr(intent, 'request', None)
    deadline = deadline.deadline if deadline else datetime.now(timezone.utc) + timedelta(seconds=20)
    job = shop.network(intent, deadline=deadline, request_key=uuid.uuid4().hex)
    while True:
        receipt = shop.network_result(job['id'])
        if receipt['state'] not in ('queued', 'running'):
            if receipt['state'] != 'succeeded':
                raise RuntimeError(str(receipt))
            return receipt['result']
        time.sleep(.2)
page = read(DirectoryRead(cloud='directory', directory_id=inputs['directory'], query=SupplierSearch.model_validate(inputs['search'])))
answers = []
for candidate in page['candidates']:
    contract = dict(inputs['query'], expected_revision=candidate['source_revision'])
    envelope = PeerRequest(instance_id=candidate['instance_id'], request_id=uuid.uuid4().hex,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=20),
        query=PeerListing(shop_id=candidate['shop_id'], listing_id=candidate['listing']['id'],
            expected_generation=candidate['source_generation'], query=ListingQuery.model_validate(contract)))
    intent = DirectRead(origin=candidate['direct_origin'], request=envelope) if candidate['direct_origin'] else RelayRead(cloud='directory', request=envelope)
    reply = read(intent)
    answers.append({'shop': candidate['shop_id'], 'outcome': reply['result']['value']['outcome'], 'via': intent.kind})
blocked = False
try:
    with socket.create_connection(('1.1.1.1', 443), timeout=.1):
        pass
except OSError:
    blocked = True
result = {'answers': answers, 'network_blocked': blocked, 'credentials_absent': not Path('/run/chipload/credentials').exists(), 'database_absent': not Path('/data').exists()}
'''


@unittest.skipUnless(os.environ.get("CHIPLOAD_FEDERATION_TESTS") == "1", "requires worker, runtime, TLS and OpenBao images")
class FederationLiveTests(unittest.TestCase):
    def test_three_instances_real_identity_supplier_selection_withdrawal_expiry_and_outage(self):
        with tempfile.TemporaryDirectory(prefix="chipload-federation-") as temporary:
            root = Path(temporary)
            name = "chipload-fed-" + uuid.uuid4().hex[:10]
            nodes, connectors, joins, networks = {}, [], [], []
            cloud_state, bao_root = root / "cloud", root / "bao"
            bao_root.mkdir(mode=0o700)
            bao_network = name + "-secrets"
            def network(deployment, key, *, internal=False):
                network_name = deployment + "_" + key
                run("docker", "network", "create", "--subnet", stack.choose_subnet(),
                    "--label", "com.docker.compose.project=" + deployment,
                    "--label", "com.docker.compose.network=" + key,
                    *(["--internal"] if internal else []), network_name)
                networks.append(network_name)
                return network_name

            bao_network = network(name, "secrets", internal=True)
            cloud_origin = f"https://cloud.localhost:{free_port()}"
            directory_id = uuid.uuid4().hex
            workload_specs = {key: {"subject": str(uuid.uuid4()), "secret": secrets.token_urlsafe(32)}
                              for key in ("dispatcher", "near-owner", "far-owner")}

            def fed(state, action, *arguments):
                return run(sys.executable, ROOT / "scripts/federation", "--state", state, action, *arguments)

            def phase(message):
                print("federation: " + message, flush=True)

            try:
                phase("starting three independent shop APIs and identity stores")
                for short, shop_id in (("buyer", "mill"), ("near", "supplier-near"), ("far", "supplier-far")):
                    state = root / short
                    cli = [sys.executable, ROOT / "scripts/tls-stack", "--state", state]
                    deployment_name = name + "-" + short
                    network(deployment_name, "edge")
                    network(deployment_name, "database", internal=True)
                    run(*cli, "init", "--local", "--name", deployment_name, "--shop", shop_id,
                        "--api-host", short + ".localhost", "--identity-host", short + "-id.localhost",
                        "--port", free_port(), "--admin-port", free_port())
                    settings = ServerSettings.model_validate_json((state / "runtime.json").read_bytes())
                    identifier = uuid.uuid4().hex
                    if short == "buyer":
                        settings = settings.model_copy(update={"bindings": (*settings.bindings, Principal(
                            subject=workload_specs["dispatcher"]["subject"], grants=(Grant(shop_id="mill", actions=("network:dispatch",)),)))})
                        realm = json.loads((state / "realm.json").read_bytes())
                        template = next(item for item in realm["clients"] if item["clientId"] == "chipload-agent")
                        for key, spec in workload_specs.items():
                            client = copy.deepcopy(template)
                            client.update(clientId=key, secret=spec["secret"])
                            audience = settings.resource_url if key == "dispatcher" else cloud_origin + "/mcp"
                            for mapper in client["protocolMappers"]:
                                if mapper["name"] == "chipload-resource-uri":
                                    mapper["config"]["included.custom.audience"] = audience
                            realm["clients"].append(client)
                            realm["users"].append({"id": spec["subject"], "username": "service-account-" + key,
                                "enabled": True, "serviceAccountClientId": key,
                                "clientRoles": {"chipload": ["shop:connect"] if key == "dispatcher" else []}})
                        private_json(state / "realm.json", realm)
                    private_json(state / "runtime.json", settings.model_copy(update={"instance_id": identifier}))
                    nodes[short] = {"state": state, "cli": cli, "name": deployment_name, "id": identifier, "shop_id": shop_id}
                    run(*cli, "up")
                    connection = json.loads((state / "client.json").read_bytes())
                    values = stack.configuration(json.loads((state / "deployment.json").read_bytes()))
                    cached = {}

                    def token(state=state, values=values, cached=cached):
                        if time.monotonic() >= cached.get("until", 0):
                            status, value = credentials.agent_token(state, values, (state / "secrets/agent-secret").read_text())
                            self.assertEqual(status, 200, "shop agent token refused")
                            cached.update(token=value["access_token"], until=time.monotonic() + value["expires_in"] - 10)
                        return cached["token"]

                    origin, ca = connection["resource_url"][:-4], Path(connection["ca_file"])
                    shop = Client.connect(origin, ca_file=ca, token=token).shop(shop_id)
                    saved = shop.save(project(shop_id), expected_head=None)
                    job = shop.build(saved["revision"], request_key="initial-build")
                    built = wait_for(lambda: shop.build_result(job["id"]), lambda value: value["state"] in ("succeeded", "failed"))
                    self.assertEqual(built["state"], "succeeded", "isolated shop source build failed")
                    shop.publish(job["id"], expected_generation=0)
                    nodes[short].update(origin=origin, ca=ca, shop=shop, job=job, token=token, settings=settings,
                        gateway=run("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={deployment_name}",
                            "--filter", "label=com.docker.compose.service=gateway"))

                phase("provisioning separate least-privilege credentials in real TLS OpenBao")
                gpg, bao_args = fixture_args(bao_root, name=name + "-bao", network=bao_network)
                bao_stack.configure(bao_args)
                bao_stack.operate(SimpleNamespace(state=bao_args.state, action="up"))
                value, _, _, container = bao_stack.deployment(bao_args.state)
                bao_stack.initialize(bao_args.state, value, container)
                bootstrap = json.loads((bao_args.state / "bootstrap.json").read_bytes())

                def decrypt(encrypted):
                    return subprocess.check_output([*gpg, "--decrypt"], input=base64.b64decode(encrypted), stderr=subprocess.DEVNULL).decode().strip()

                for index, encrypted in enumerate(bootstrap["keys_base64"][:2]):
                    share = root / f"share-{index}"
                    stack.write(share, decrypt(encrypted))
                    bao_stack.operate(SimpleNamespace(state=bao_args.state, action="unseal", share_file=share))
                root_token = decrypt(bootstrap["root_token"])

                def bao(method, path, body=None, token=root_token, allowed_statuses=()):
                    return bao_stack.request(bao_args.state, value, container, method, path, body, token, allowed_statuses=allowed_statuses)

                bao("POST", "sys/mounts/connector", {"type": "kv", "options": {"version": "2"}})
                bao("POST", "sys/auth/approle", {"type": "approle"})
                for key, spec in workload_specs.items():
                    bao("POST", f"connector/data/{key}", {"data": {"client-secret": spec["secret"]}})
                    bao("PUT", "sys/policies/acl/" + key, {"policy": f'path "connector/data/{key}" {{ capabilities = ["read"] }}\npath "auth/token/revoke-self" {{ capabilities = ["update"] }}'})
                    bao("POST", "auth/approle/role/" + key, {"token_policies": [key], "token_no_default_policy": True,
                        "token_ttl": "60s", "token_max_ttl": "60s", "secret_id_ttl": "1h"})
                    role = bao("GET", "auth/approle/role/" + key + "/role-id")[1]["data"]["role_id"]
                    secret = bao("POST", "auth/approle/role/" + key + "/secret-id", {})[1]["data"]["secret_id"]
                    credential_dir = root / (key + "-credentials")
                    credential_dir.mkdir(mode=0o700)
                    stack.write(credential_dir / "role-id", role)
                    stack.write(credential_dir / "secret-id", secret)
                    stack.write(credential_dir / "bao-ca.pem", (bao_args.state / "certificates/ca.pem").read_bytes())
                    spec["credentials"] = credential_dir
                    scoped = bao("POST", "auth/approle/login", {"role_id": role, "secret_id": secret}, token=None)[1]["auth"]["client_token"]
                    self.assertEqual(bao("GET", f"connector/data/{key}", token=scoped)[0], 200)
                    for method, path, body in (("GET", "sys/mounts", None), ("GET", "connector/data/unrelated", None),
                                               ("POST", f"connector/data/{key}", {"data": {"bad": True}})):
                        self.assertEqual(bao(method, path, body, token=scoped, allowed_statuses=(403,))[0], 403)
                    bao("POST", "auth/token/revoke-self", {}, token=scoped)
                bao("POST", "auth/token/revoke-self", {})
                self.assertEqual(bao("GET", "sys/mounts", allowed_statuses=(403,))[0], 403)

                buyer, near, far = (nodes[key] for key in ("buyer", "near", "far"))
                cloud_tls = root / "cloud-tls"
                stack.local_certificates(cloud_tls, {"api": "cloud.localhost", "identity": "cloud.localhost"})
                bundle = root / "connector-ca.pem"
                stack.write(bundle, b"\n".join(node["ca"].read_bytes() for node in nodes.values()) + b"\n" + (cloud_tls / "identity-ca.pem").read_bytes())

                def policy(origins, *, route=True, ca=bundle, seconds=15):
                    return EgressPolicy(private_origins=tuple(origins), ca_file=ca, seconds=seconds,
                        routes=tuple(PrivateRoute(origin=origin, port=8443) for origin in origins if route and origin != "https://bao:8200"))

                issuer = buyer["settings"].issuer
                identity_origin = issuer.removesuffix("/realms/chipload")
                cloud_config = CloudSettings(directory_id=directory_id, identity=ServerSettings(database=root / "unused.db",
                    resource_url=cloud_origin + "/mcp", issuer=issuer, jwks_url=buyer["settings"].jwks_url,
                    jwks_ca_file=buyer["ca"]), bindings=tuple(OwnerBinding(subject=workload_specs[key + "-owner"]["subject"],
                        instance_id=nodes[key]["id"]) for key in ("near", "far")), egress=policy([near["origin"]]))
                private_json(root / "cloud.json", cloud_config)
                network(name + "-cloud", "edge")
                network(name + "-cloud", "egress")
                fed(cloud_state, "prepare", "--role", "cloud", "--name", name + "-cloud", "--config", root / "cloud.json", "--certificates", cloud_tls)
                fed(cloud_state, "initialize")
                fed(cloud_state, "up")

                def join(network, node, alias):
                    run("docker", "network", "connect", "--alias", alias, network, node["gateway"])
                    joins.append((network, node["gateway"]))

                join(name + "-cloud_egress", buyer, "buyer-id.localhost")
                join(name + "-cloud_egress", near, "near.localhost")
                join(name + "-cloud_edge", near, "near.localhost")
                for short, node in nodes.items():
                    key = "dispatcher" if short == "buyer" else short + "-owner"
                    spec = workload_specs[key]
                    trust = ServerSettings(database=root / "unused.db", issuer=issuer,
                        resource_url=(node["origin"] if short == "buyer" else cloud_origin) + "/mcp", jwks_url=issuer + "/protocol/openid-connect/certs")
                    workload = WorkloadIdentity(client_id=key, subject=spec["subject"], trust=trust,
                        secrets=OpenBao(origin="https://bao:8200", ca_file="bao-ca.pem", kv_mount="connector", secret_path=key, field="client-secret", policy=key),
                        policy=policy([identity_origin, cloud_origin, "https://bao:8200", *([node["origin"]] if short == "buyer" else [])]))
                    link = CloudLink(id="directory", origin=cloud_origin, directory_id=directory_id,
                        advertise=short != "buyer", relay=short != "buyer", policy=policy([cloud_origin]),
                        identity=workload if short != "buyer" else None, credentials=spec["credentials"] if short != "buyer" else None)
                    config = ConnectorSettings(instance_id=node["id"], source_origin=node["origin"], source_policy=policy([node["origin"]]),
                        public_origin=node["origin"] if short == "near" else None,
                        query_policy=policy([near["origin"]]), shops=(node["shop_id"],), clouds=(link,),
                        dispatch=DispatchLink(credentials=spec["credentials"], identity=workload) if short == "buyer" else None,
                        renew_seconds=10, lease_seconds=30)
                    config_path, connector_state = root / (short + "-connector.json"), root / (short + "-connector")
                    private_json(config_path, config)
                    network(name + "-" + short + "-connector", "egress")
                    fed(connector_state, "prepare", "--role", "connector", "--config", config_path, "--name", name + "-" + short + "-connector",
                        "--network", node["name"] + "_application", "--network", bao_network, "--network", name + "-cloud_edge")
                    # Identity provider has no public host route from a container: explicit private join.
                    # The buyer gateway is visible through its application network for buyer, and via cloud edge for suppliers.
                    connectors.append(connector_state)
                    fed(connector_state, "up")
                join(name + "-cloud_edge", buyer, "buyer-id.localhost")
                cloud = CloudClient(cloud_origin, directory_id, policy=policy([cloud_origin], route=False))
                search = SupplierSearch(profile=term("material-supply"), inputs=query(revision="a" * 64, count="5").inputs[:3],
                    origin=GeoPoint(latitude="41.88", longitude="-87.63", accuracy_m=100), radius_m=100_000)
                phase("waiting for authenticated publication and outbound relay enrollment")
                page = wait_for(lambda: cloud.search(search), lambda value: len(value.candidates) == 2 and all(c.relay_available for c in value.candidates))
                self.assertEqual([c.shop_id for c in page.candidates], ["supplier-near", "supplier-far"])
                self.assertIsNone(page.candidates[1].direct_origin)
                self.assertLess(page.candidates[0].distance_m, page.candidates[1].distance_m)
                direct = PeerClient(near["origin"], near["id"], policy=policy([near["origin"]], route=False))
                near_query = query(revision=page.candidates[0].source_revision, count="5")
                self.assertEqual(direct.evaluate_listing("supplier-near", "service", near_query, expected_generation=1).value["outcome"], "unsupported")
                self.assertEqual(cloud.evaluate_listing(near["id"], "supplier-near", "service", near_query, expected_generation=1).value["outcome"], "unsupported")
                self.assertEqual(cloud.evaluate_listing(far["id"], "supplier-far", "service",
                    query(revision=page.candidates[1].source_revision, count="5"), expected_generation=1).value["outcome"], "supported")
                phase("comparing ordinary REST and actual MCP hosted Python supplier selection")
                program_inputs = {"directory": directory_id, "search": search.model_dump(mode="json"),
                                  "query": query(revision="a" * 64, count="5").model_dump(mode="json")}
                expected = {"answers": [{"shop": "supplier-near", "outcome": "unsupported", "via": "direct"},
                                         {"shop": "supplier-far", "outcome": "supported", "via": "relay"}],
                            "network_blocked": True, "credentials_absent": True, "database_absent": True}
                job = buyer["shop"].run(SUPPLIER_PROGRAM, request_key="rest-supplier-search", inputs=program_inputs)
                receipt = wait_for(lambda: buyer["shop"].run_result(job["id"]), lambda value: value["state"] not in ("queued", "running"))
                self.assertEqual(receipt["state"], "succeeded", str(receipt))
                self.assertEqual(receipt["value"], expected)

                async def mcp_search():
                    context = ssl.create_default_context(cafile=buyer["ca"])
                    async with httpx2.AsyncClient(verify=context, trust_env=False, headers={"Authorization": "Bearer " + buyer["token"]()}) as http:
                        async with MCPClient(streamable_http_client(buyer["origin"] + "/mcp", http_client=http)) as agent:
                            tools = await agent.list_tools()
                            self.assertEqual({tool.name for tool in tools.tools}, {"run_python", "read_run", "cancel_run", "discover_shop"})
                            submitted = await agent.call_tool("run_python", {"request": {"shop_id": "mill", "request_key": "mcp-supplier-search", "code": SUPPLIER_PROGRAM, "inputs": program_inputs}})
                            self.assertFalse(submitted.is_error, str(submitted))
                            for _ in range(120):
                                answer = await agent.call_tool("read_run", {"request": {"shop_id": "mill", "run_id": submitted.structured_content["id"]}})
                                self.assertFalse(answer.is_error, str(answer))
                                if answer.structured_content["state"] not in ("queued", "running"):
                                    return answer.structured_content
                                await asyncio.sleep(1)
                            self.fail("MCP supplier selection did not finish")
                receipt = asyncio.run(mcp_search())
                self.assertEqual(receipt["state"], "succeeded", str(receipt))
                self.assertEqual(receipt["value"], expected)

                phase("checking private supplier and connector container boundaries")
                for short in nodes:
                    container_id = run("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={name}-{short}-connector")
                    record = json.loads(run("docker", "inspect", container_id))[0]
                    self.assertTrue(record["HostConfig"]["ReadonlyRootfs"])
                    self.assertEqual(record["Config"]["User"], f"{os.getuid()}:{os.getgid()}")
                    self.assertEqual(record["HostConfig"]["CapDrop"], ["ALL"])
                    self.assertFalse(record["HostConfig"]["PortBindings"])
                    self.assertFalse(any(m["Destination"] in ("/data", "/var/run/docker.sock") for m in record["Mounts"]))
                    self.assertFalse(any("share-" in m["Source"] or "gnupg" in m["Source"] or m["Source"].endswith("bootstrap.json") for m in record["Mounts"]))
                far_record = json.loads(run("docker", "inspect", far["gateway"]))[0]
                self.assertNotIn(name + "-cloud_edge", far_record["NetworkSettings"]["Networks"])
                self.assertTrue(all(binding["HostIp"] == "127.0.0.1" for bindings in far_record["HostConfig"]["PortBindings"].values() for binding in bindings))

                phase("checking publication withdrawal and lease expiry")
                far["shop"].withdraw(expected_generation=1)
                with self.assertRaises(ServiceError) as removed:
                    cloud.catalog(far["id"], "supplier-far")
                self.assertEqual(removed.exception.status, 404)
                wait_for(lambda: cloud.search(search), lambda value: len(value.candidates) == 1, seconds=45)
                far["shop"].publish(far["job"]["id"], expected_generation=2)
                wait_for(lambda: cloud.search(search), lambda value: len(value.candidates) == 2, seconds=45)
                fed(connectors[2], "down")
                wait_for(lambda: cloud.search(search), lambda value: len(value.candidates) == 1, seconds=45)

                phase("checking direct dispatcher after cloud outage and connector restart")
                # Stop only the cloud process, retaining its networks for the explicit fixture joins.
                cloud_container = run("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={name}-cloud", "--filter", "label=com.docker.compose.service=cloud")
                run("docker", "stop", cloud_container)
                self.assertEqual(direct.catalog("supplier-near").generation, 1)
                fed(connectors[0], "down")
                fed(connectors[0], "up")
                deadline = datetime.now(timezone.utc) + timedelta(seconds=25)
                job = buyer["shop"].network(DirectRead(origin=near["origin"], request=PeerRequest(instance_id=near["id"],
                    request_id=uuid.uuid4().hex, deadline=deadline, query=PeerCatalog(shop_id="supplier-near"))), deadline=deadline, request_key="direct-cloud-outage")
                receipt = wait_for(lambda: buyer["shop"].network_result(job["id"]), lambda value: value["state"] not in ("queued", "running"), seconds=30)
                self.assertEqual(receipt["state"], "succeeded", str(receipt))
                self.assertEqual(receipt["result"]["result"]["value"]["shop"]["id"], "supplier-near")
                phase("all federation assertions passed")
            finally:
                for state in reversed(connectors):
                    fed(state, "down")
                for network, gateway in reversed(joins):
                    run("docker", "network", "disconnect", network, gateway)
                if (cloud_state / "deployment.json").exists():
                    fed(cloud_state, "down")
                if (bao_root / "state/deployment.json").exists():
                    bao_stack.operate(SimpleNamespace(state=bao_root / "state", action="down"))
                for node in reversed(list(nodes.values())):
                    run(*node["cli"], "down")
                    volumes = run("docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={node['name']}").split()
                    if volumes:
                        run("docker", "volume", "rm", *volumes)
                remaining = set(run("docker", "network", "ls", "--format", "{{.Name}}").split())
                for network_name in reversed(networks):
                    if network_name in remaining:
                        run("docker", "network", "rm", network_name)
