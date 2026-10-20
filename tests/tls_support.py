"""Load the operator TLS helper without executing its CLI."""

import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("tls_stack_script", str(ROOT / "scripts/tls-stack"))
stack = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(stack)
