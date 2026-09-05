"""Pytest configuration for the W33-1 ingestion test suite.

The ``tests/ingestion/__init__.py`` marker (required by the W33-1
task spec) makes pytest treat ``tests/ingestion/`` as a package,
which causes pytest to insert ``tests/`` into ``sys.path``. Without
intervention, that would shadow the top-level ``ingestion/`` package
(the module under test) — Python would find ``tests/ingestion/``
first and fail with ``ModuleNotFoundError: No module named
'ingestion.pipeline'`` when a test imports ``ingestion.pipeline``.

This conftest pre-processes ``sys.path`` so the real top-level
``ingestion/`` package is found. Mirrors the convention used by the
sibling test modules (each of which inserts ``_PROJECT_ROOT`` at the
front of ``sys.path``), but centralised here so every test in the
directory benefits without re-defining the workaround.

The cleanup is idempotent: it's safe to invoke from every test module
in this directory (the ``set`` check skips work that's already been
done).

Note on sys.modules
-------------------
The test-shadowed ``ingestion`` package can NOT be dropped from
``sys.modules`` here (this conftest is itself loaded as
``ingestion.conftest`` by pytest's package-aware import — dropping
``ingestion`` mid-load would raise ``KeyError: 'ingestion.conftest'``).
The test modules' lazy import path (``from ingestion.pipeline import
Pipeline`` inside the test function body) re-resolves ``ingestion``
via sys.path — and because we ensure the project root is at the
FRONT of sys.path here, Python finds the real ``ingestion/`` package
first. We additionally clear the cache inside each test module's
top-level setup (via ``_clear_test_ingestion_cache()``) to defend
against any pre-conftest import.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Ensure the project root is at the FRONT of sys.path so the real
# ``ingestion/`` package shadows the test-shadowing
# ``tests/ingestion/`` package. ``list.remove`` + ``list.insert(0, ...)``
# (rather than the conventional ``if not in sys.path`` guard) so the
# project root is always promoted to the front even if pytest inserted
# ``tests/`` after the project-level conftest inserted the project
# root.
_path_str = str(_PROJECT_ROOT)
if _path_str in sys.path:
    sys.path.remove(_path_str)
sys.path.insert(0, _path_str)


def _clear_test_ingestion_cache() -> None:
    """Drop the test-shadowed ``ingestion`` package from sys.modules.

    Called from each test module's top-level setup (NOT from this
    conftest's top level) — at that point the conftest has finished
    loading, so the ``ingestion.conftest`` cache entry is no longer
    being populated and the delete is safe.

    Without this, Python's import cache would serve the wrong package
    even after sys.path is fixed (the cached ``ingestion`` module from
    ``tests/ingestion/__init__.py`` would be returned for every
    subsequent ``import ingestion``).
    """
    if "ingestion" in sys.modules:
        mod = sys.modules["ingestion"]
        if "tests" in str(getattr(mod, "__file__", "")) or "tests" in str(
            getattr(mod, "__path__", "")
        ):
            for key in list(sys.modules):
                if key == "ingestion" or key.startswith("ingestion."):
                    # Skip our own conftest module — it's mid-load and
                    # dropping it would raise ``KeyError``.
                    if key == "ingestion.conftest":
                        continue
                    del sys.modules[key]
