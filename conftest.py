"""Repository-level pytest guard (test harness only).

Some CLI tests execute real entry points, and those call ``logging.basicConfig(...,
force=True, stream=sys.stderr)``.  Pytest's capture stream is closed when the owning
test finishes, so the root handler that was installed then points at a closed file and
the next INFO record anywhere in the suite raises ``ValueError: I/O operation on
closed file`` ("--- Logging error ---"), which the G7 clean-shell gate forbids.

This fixture removes only root handlers whose stream is already closed, before and
after each test.  It changes no production logging, no logger level and no test
assertion; it merely drops a handler that can no longer write anywhere.
"""

from __future__ import annotations

import logging

import pytest


def _drop_closed_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        stream = getattr(handler, "stream", None)
        if stream is not None and getattr(stream, "closed", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - cleanup must never fail a test
                pass


@pytest.fixture(autouse=True)
def _no_closed_logging_handlers():
    _drop_closed_handlers()
    yield
    _drop_closed_handlers()
