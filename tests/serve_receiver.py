"""Entrypoint for the private receiver integration fixture."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("receiver_support", "/fixture/receiver_support.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
server = module.Receiver(
    ("0.0.0.0", 8443),
    Path("/receiver/receipts.db"),
    "loaded-from-control",
    certificate="/tls/receiver.crt",
    key="/tls/receiver.key",
    control="/receiver/control.json",
)
server.serve_forever()
