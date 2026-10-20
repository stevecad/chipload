"""Load repository operator recovery tools without invoking their CLIs."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "recovery_archive", ROOT / "scripts/recovery_archive.py"
)
archive = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = archive
spec.loader.exec_module(archive)
loader = importlib.machinery.SourceFileLoader(
    "tls_recovery_script", str(ROOT / "scripts/tls-recovery")
)
script = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(script)
