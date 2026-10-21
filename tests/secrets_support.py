"""Load the private OpenBao deployment tool and generate test-only PGP recipients."""

import importlib.machinery
import importlib.util
import subprocess
from types import SimpleNamespace

from recovery_support import ROOT, archive

loader = importlib.machinery.SourceFileLoader(
    "secrets_stack_script", str(ROOT / "scripts/secrets-stack")
)
script = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(script)


def recipients(root):
    home = root / "gnupg"
    home.mkdir(mode=0o700)
    command = [
        "gpg",
        "--homedir",
        str(home),
        "--batch",
        "--pinentry-mode",
        "loopback",
        "--passphrase",
        "",
    ]
    paths = []
    for index in range(3):
        name = f"Chipload fixture {index} <fixture{index}@example.invalid>"
        subprocess.run(
            [*command, "--quick-generate-key", name, "rsa2048", "encr", "1d"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        path = root / f"recipient-{index}.pgp"
        archive.write(
            path, subprocess.check_output([*command, "--export", name], stderr=subprocess.DEVNULL)
        )
        paths.append(path)
    return command, paths


def fixture_args(root, *, name="chipload-secrets-test", network="secrets-test"):
    material = root / "tls"
    script.tls.local_certificates(material, {"server": "bao"})
    command, keys = recipients(root)
    return command, SimpleNamespace(
        state=root / "state",
        name=name,
        network=network,
        host="bao",
        certificates=material / "certificates",
        ca_bundle=material / "identity-ca.pem",
        pgp_key=keys,
        root_pgp_key=keys[0],
        threshold=2,
    )
