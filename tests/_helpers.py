"""Shared helpers for loading the numbered pipeline scripts as modules."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_cache = {}


def load_step(filename: str):
    """Import a pipeline script such as ``7_resolve_entities.py`` by filename."""
    if filename not in _cache:
        name = "step_" + filename.removesuffix(".py")
        spec = importlib.util.spec_from_file_location(name, ROOT / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _cache[filename] = module
    return _cache[filename]


class FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by mocked HTTP calls."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")
