"""Load the TLS certificate operator with its recovery dependencies."""

import importlib.machinery
import importlib.util

from recovery_support import ROOT, archive

loader = importlib.machinery.SourceFileLoader(
    "tls_certificates_script", str(ROOT / "scripts/tls-certificates")
)
certificates = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(certificates)

__all__ = ["ROOT", "archive", "certificates"]
