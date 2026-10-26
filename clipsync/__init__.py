"""clipsync — pluggable two-way clipboard sync prototype.

The package is deliberately split into a protocol model (validation of untrusted wire
data), clipboard backends (a fake for tests and an unexecuted Wayland backend),
transports (in-memory and local-file fakes), an optional encryption codec, and a sync
engine that ties them together.  Nothing here touches the real clipboard unless a
caller explicitly uses :class:`clipsync.clipboard.WlClipboardBackend`.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
