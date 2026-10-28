#!/usr/bin/env python3
"""Generate fresh clipsync secrets for one group.

Prints a random shared token (>= 32 characters) and a fresh Fernet key.  Nothing
is written unless ``--write FILE`` is given; that file is created with mode 0600
and an existing file is never overwritten.

Examples
--------
    python3 scripts/gen_secrets.py
    python3 scripts/gen_secrets.py --write ~/.config/clipsync/family-a.secrets

The written file is a secret note, not a daemon config: copy the token into the
member config (and into the relay's ``[[serve_groups]]`` entry for that group),
and the key into the member config's ``e2e_key``.  The relay never gets the key.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def generate() -> tuple[str, str]:
    """Return ``(token, fernet_key)`` as fresh random strings."""
    token = secrets.token_urlsafe(32)
    key = Fernet.generate_key().decode("ascii")
    return token, key


def render(token: str, key: str) -> str:
    return (
        "# clipsync group secrets -- generated locally, keep out of git\n"
        "# mode 0600; the relay gets the token only, never the e2e key\n"
        f'token = "{token}"\n'
        f'e2e_key = "{key}"\n'
    )


def write_new_file(path: Path, text: str) -> None:
    """Create *path* with mode 0600, refusing to overwrite an existing file."""
    path = path.expanduser()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite existing file: {path}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print or write fresh clipsync group secrets (token + Fernet key).",
    )
    parser.add_argument(
        "--write",
        metavar="FILE",
        type=Path,
        help="create FILE with mode 0600 instead of printing (never overwrites)",
    )
    args = parser.parse_args(argv)

    token, key = generate()
    text = render(token, key)

    if args.write is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return 0

    try:
        write_new_file(args.write, text)
    except FileExistsError as exc:
        sys.stderr.write(f"gen_secrets: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"gen_secrets: cannot write {args.write}: {exc}\n")
        return 1

    sys.stdout.write(f"wrote {args.write} (mode 0600); keep it out of git\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
