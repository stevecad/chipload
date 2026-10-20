"""Load the credential operator helper with its recovery dependencies."""

import importlib.machinery
import importlib.util

from recovery_support import ROOT, archive

loader = importlib.machinery.SourceFileLoader(
    "tls_credentials_script", str(ROOT / "scripts/tls-credentials")
)
credentials = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(credentials)

__all__ = ["ROOT", "archive", "credentials"]
