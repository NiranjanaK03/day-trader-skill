"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name, market="us"):
    """The upstream tests replay US sessions (TSLA, NYSE, New York time), so a
    script loads on the "us" market unless a test asks for "in" (NSE, India time)."""
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_%s_%s" % (name, market), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, "set_market"):
        mod.set_market(market)
    return mod
