"""Load ACME tooling and provide a local CA that performs real HTTP validation."""

import contextlib
import http.client
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

from recovery_support import ROOT, archive

environment_spec = importlib.util.spec_from_file_location(
    "acme_environment", ROOT / "scripts/acme_environment.py"
)
environment = importlib.util.module_from_spec(environment_spec)
sys.modules[environment_spec.name] = environment
environment_spec.loader.exec_module(environment)

loader = importlib.machinery.SourceFileLoader("tls_acme_script", str(ROOT / "scripts/tls-acme"))
acme = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(acme)


def command(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} failed: {result.stderr[-4000:]}")
    return result.stdout.strip()


@contextlib.contextmanager
def authority(root):
    name = "chipload-acme-test-" + uuid.uuid4().hex[:12]
    network = name + "-network"
    subnet = ipaddress.ip_network(acme.stack.choose_subnet())
    address = str(subnet[1])
    domain = "acme.example.test"
    hosts = {"api": "shop.example.test", "identity": "identity.example.test"}
    server = root / "server"
    acme.stack.local_certificates(server, {"api": domain})
    command("docker", "network", "create", "--subnet", str(subnet), network)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        management_port = listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind((address, 0))
        http_port = listener.getsockname()[1]
    configuration = {
        "pebble": {
            "listenAddress": "0.0.0.0:14000",
            "managementListenAddress": "0.0.0.0:15000",
            "certificate": "/config/server.crt",
            "privateKey": "/config/server.key",
            "httpPort": http_port,
            "tlsPort": 5001,
            "keyAlgorithm": "ecdsa",
            "profiles": {
                "default": {
                    "description": "Renewal integration fixture",
                    "validityPeriod": 3 * 86400,
                }
            },
        }
    }
    archive.write(server / "pebble.json", json.dumps(configuration))
    arguments = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        domain,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "256m",
        "--pids-limit",
        "64",
        "--env",
        "PEBBLE_VA_NOSLEEP=1",
        "--env",
        "PEBBLE_WFE_NONCEREJECT=0",
        "--env",
        "PEBBLE_AUTHZREUSE=0",
        "--publish",
        f"127.0.0.1:{management_port}:15000",
    ]
    for hostname in hosts.values():
        arguments.extend(("--add-host", f"{hostname}:{address}"))
    for source, target in (
        (server / "certificates/api.crt", "server.crt"),
        (server / "certificates/api.key", "server.key"),
        (server / "pebble.json", "pebble.json"),
    ):
        arguments.extend(("--mount", f"type=bind,source={source},target=/config/{target},readonly"))
    arguments.extend((acme.IMAGES["test_ca"], "-config", "/config/pebble.json", "-strict=true"))
    try:
        command(*arguments)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=server / "identity-ca.pem")
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", management_port), timeout=3) as raw:
                    with context.wrap_socket(raw, server_hostname=domain) as secured:
                        secured.sendall(
                            f"GET /roots/0 HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode()
                        )
                        response = http.client.HTTPResponse(secured)
                        response.begin()
                        if response.status != 200:
                            raise RuntimeError("test CA management endpoint failed")
                        root_ca = response.read(1024 * 1024)
                break
            except OSError:
                record = json.loads(command("docker", "inspect", name))[0]
                if not record["State"]["Running"]:
                    logs = subprocess.run(
                        ["docker", "logs", "--tail", "20", name],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    raise RuntimeError("test CA exited: " + logs.stdout + logs.stderr) from None
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        trust = root / "issuer-ca.pem"
        archive.write(trust, root_ca)
        yield SimpleNamespace(
            root=root,
            name=name,
            network=network,
            address=address,
            http_port=http_port,
            hosts=hosts,
            server_url=f"https://{domain}:14000/dir",
            directory_ca=server / "identity-ca.pem",
            ca_bundle=trust,
        )
    finally:
        issuer_names = command(
            "docker", "ps", "--all", "-q", "--filter", "network=" + network
        ).split()
        if issuer_names:
            command("docker", "rm", "--force", *issuer_names)
        command("docker", "network", "rm", network)


def initialize(state, fixture, *, renew_days=0):
    acme.initialize(
        SimpleNamespace(
            state=state,
            api_host=fixture.hosts["api"],
            identity_host=fixture.hosts["identity"],
            server=fixture.server_url,
            email="operator@example.test",
            listen_address=fixture.address,
            http_port=fixture.http_port,
            network=fixture.network,
            accept_terms=True,
            ca_bundle=fixture.ca_bundle,
            directory_ca=fixture.directory_ca,
            renew_days=renew_days,
        )
    )


__all__ = ["ROOT", "archive", "acme", "authority", "command", "initialize"]
