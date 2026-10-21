"""Load the issuer recovery CLI and its bounded archive reader."""

import importlib.machinery
import importlib.util
import sys

from acme_support import ROOT, archive

spec = importlib.util.spec_from_file_location("acme_archive", ROOT / "scripts/acme_archive.py")
bundle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bundle
spec.loader.exec_module(bundle)
loader = importlib.machinery.SourceFileLoader(
    "acme_recovery_script", str(ROOT / "scripts/tls-acme-recovery")
)
recovery = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(recovery)
acme = recovery.acme

__all__ = ["ROOT", "archive", "bundle", "recovery", "acme"]
