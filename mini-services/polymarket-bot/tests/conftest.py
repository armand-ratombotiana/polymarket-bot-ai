"""
Project-local pytest configuration for the polymarket-bot test suite.

Anchors the test root (so ``from core.<module> import ...`` resolves
without sys.path gymnastics) and applies ``@pytest.mark.asyncio`` to every
``async def test_...`` function in the package via the module-level
``pytestmark`` declaration in each test module.

The repo's ``pytest.ini`` declares ``testpaths = tests`` — this file makes
that discovery work even though the project's ``pyproject.toml`` /
``pytest.ini`` are intentionally left untouched (the S9 task spec
constrains us to *new* files only).
"""
