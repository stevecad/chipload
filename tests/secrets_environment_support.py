"""Load image identification helpers without invoking Docker during discovery."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "secrets_environment", ROOT / "scripts/secrets_environment.py"
)
environment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = environment
spec.loader.exec_module(environment)
