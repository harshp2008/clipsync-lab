"""Shared pytest setup for the clipsync-lab suite (test-side only).

A session-scoped autouse fixture raises the third-party ``websockets`` logger to
WARNING while tests run.  The library logs one INFO line per connect/close; those
records can hit pytest's capture stream just as it is being suspended at the end of
a test, which produces the noisy ``--- Logging error --- ... I/O operation on closed
file`` traceback.  This changes no production logging and no test-level assertion.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(scope="session", autouse=True)
def _quiet_websockets_logger():
    logger = logging.getLogger("websockets")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)
