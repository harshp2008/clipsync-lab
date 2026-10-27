"""Peer command and helpers for clipsync without accessing clipboard."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, Sequence, TextIO

import clipsync
from .config import ConfigError, DaemonConfig, load_config
from .crypto import Codec, CryptoError
from .daemon import BackoffPolicy, build_codec, build_transport, wait_or_stop
from .protocol import (
    ClipError,
    ClipMessage,
    ClipPayload,
    ValidationError,
    decode_frame,
    encode_frame,
    image_payload,
    text_payload,
)
from .transport import Transport, TransportError

__all__ = [
    "IMAGE_MIME_BY_SUFFIX",
    "PeerError",
    "image_payload_from_file",
    "listen",
    "main",
    "send_payload",
]

logger = logging.getLogger("clipsync.peer")

IMAGE_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


class PeerError(Exception):
    """Base exception for peer command errors."""


def image_payload_from_file(path: str | Path) -> ClipPayload:
    """Read an image file and construct a ClipPayload.

    Raises PeerError on unreadable, missing, unsupported or invalid image file.
    Never exposes payload bytes in exception message.
    """
    path_obj = Path(path).expanduser()
    suffix = path_obj.suffix.lower()
    if suffix not in IMAGE_MIME_BY_SUFFIX:
        raise PeerError(f"Unsupported image file suffix: {suffix}")
    mime = IMAGE_MIME_BY_SUFFIX[suffix]

    try:
        data = path_obj.read_bytes()
    except OSError as exc:
        raise PeerError(f"Could not read image file {path_obj.name}: {exc}") from exc

    try:
        return image_payload(data, mime)
    except (ValidationError, ClipError) as exc:
        raise PeerError(f"Invalid image file {path_obj.name}: {exc}") from exc


def send_payload(
    transport: Transport,
    codec: Codec | None,
    device_id: str,
    payload: ClipPayload,
    *,
    seq: int | None = None,
) -> ClipMessage:
    """Encode, optionally encrypt, and send a payload over transport."""
    if seq is None:
        seq = int(time.time() * 1000)

    message = ClipMessage(origin=device_id, seq=seq, payload=payload)
    frame = encode_frame(message)
    if codec is not None:
        frame = codec.encrypt(frame)

    transport.send(frame)
    return message


def listen(
    transport: Transport,
    codec: Codec | None,
    *,
    device_id: str,
    output_dir: str | Path,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    backoff: BackoffPolicy | None = None,
    out: TextIO | None = None,
) -> int:
    """Listen for incoming payloads and write JSON lines to out.

    Images are saved to output_dir with their message ID as filename.
    Returns number of payloads printed.
    """
    out_path = Path(output_dir).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    if backoff is None:
        backoff = BackoffPolicy()
    if out is None:
        out = sys.stdout

    count = 0
    consecutive_failures = 0

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        if not transport.connected:
            try:
                transport.connect()
            except TransportError as exc:
                logger.warning("Peer transport connect failed: %s", exc)
                delay = backoff.delay(consecutive_failures)
                consecutive_failures += 1
                if wait_or_stop(stop_event, delay, sleep=sleep):
                    break
                continue
            else:
                consecutive_failures = 0

        try:
            frames = transport.recv()
        except TransportError as exc:
            logger.warning("Peer transport recv failed: %s", exc)
            delay = backoff.delay(consecutive_failures)
            consecutive_failures += 1
            if wait_or_stop(stop_event, delay, sleep=sleep):
                break
            continue
        else:
            consecutive_failures = 0

        for frame in frames:
            raw = frame
            if codec is not None:
                try:
                    raw = codec.decrypt(frame)
                except CryptoError as exc:
                    logger.warning("Frame decryption failed: %s", exc)
                    continue

            try:
                msg = decode_frame(raw)
            except ValidationError as exc:
                logger.warning("Frame decode failed: %s", exc)
                continue

            if msg.origin == device_id:
                continue

            if msg.payload.kind == "text":
                line_obj: dict[str, Any] = {
                    "kind": "text",
                    "mime": msg.payload.mime,
                    "origin": msg.origin,
                    "seq": msg.seq,
                    "text": msg.payload.text,
                }
            elif msg.payload.kind == "image":
                ext = _MIME_TO_EXT.get(msg.payload.mime, "bin")
                img_file = out_path / f"{msg.msg_id}.{ext}"
                img_file.write_bytes(msg.payload.data or b"")
                line_obj = {
                    "bytes": len(msg.payload.data or b""),
                    "kind": "image",
                    "mime": msg.payload.mime,
                    "origin": msg.origin,
                    "path": str(img_file.resolve()),
                    "seq": msg.seq,
                }
            else:
                continue

            out.write(
                json.dumps(
                    line_obj,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            count += 1

        if stop_event is not None and stop_event.is_set():
            break

        if wait_or_stop(stop_event, poll_interval, sleep=sleep):
            break

    return count


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipsync-peer",
        description="Peer CLI tool to send and listen for clipboard payloads.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="~/.config/clipsync/config.toml",
        help="Path to TOML configuration file (default: ~/.config/clipsync/config.toml)",
    )
    parser.add_argument(
        "--device-id",
        type=str,
        default=None,
        help="Override device_id from configuration.",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {clipsync.__version__}",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    send_text_p = subparsers.add_parser("send-text", help="Send text payload.")
    send_text_p.add_argument("text", type=str, help="Text to send.")

    send_image_p = subparsers.add_parser("send-image", help="Send image file.")
    send_image_p.add_argument("file", type=str, help="Path to image file.")

    listen_p = subparsers.add_parser("listen", help="Listen for remote payloads.")
    listen_p.add_argument(
        "--output-dir",
        type=str,
        default="clipsync-images",
        help="Directory to save received images (default: clipsync-images).",
    )

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[DaemonConfig], Transport] | None = None,
    codec_factory: Callable[[DaemonConfig], Codec | None] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """CLI entry point for clipsync-peer."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    try:
        config_path = Path(args.config).expanduser()
        config = load_config(config_path)
    except ConfigError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        return 2

    if args.device_id is not None:
        device_id = args.device_id.strip()
        if not (1 <= len(device_id) <= 128):
            sys.stderr.write("device_id must be between 1 and 128 characters.\n")
            return 2
    else:
        device_id = config.device_id

    # Pre-validate payload before network connections
    payload: ClipPayload | None = None
    if args.subcommand == "send-text":
        try:
            payload = text_payload(args.text)
        except (ValidationError, ClipError, ValueError) as exc:
            sys.stderr.write(f"Invalid text payload: {exc}\n")
            return 2
    elif args.subcommand == "send-image":
        try:
            payload = image_payload_from_file(args.file)
        except PeerError as exc:
            sys.stderr.write(f"Invalid image file: {exc}\n")
            return 2

    make_transport = transport_factory or build_transport
    make_codec = codec_factory or build_codec

    transport = make_transport(config)
    codec = make_codec(config)

    if args.subcommand in ("send-text", "send-image"):
        assert payload is not None
        try:
            transport.connect()
            send_payload(transport, codec, device_id, payload)
        except TransportError as exc:
            sys.stderr.write(f"Transport error: {exc}\n")
            return 1
        finally:
            transport.close()
        return 0

    if args.subcommand == "listen":
        backoff = BackoffPolicy(
            base=config.reconnect_delay,
            max_delay=30.0,
            jitter=0.5,
        )

        if stop_event is None:
            local_stop = threading.Event()

            def _sig_handler(_signum: int, _frame: Any) -> None:
                local_stop.set()

            try:
                signal.signal(signal.SIGINT, _sig_handler)
                signal.signal(signal.SIGTERM, _sig_handler)
            except (ValueError, AttributeError):
                pass
            run_stop = local_stop
        else:
            run_stop = stop_event

        try:
            listen(
                transport,
                codec,
                device_id=device_id,
                output_dir=args.output_dir,
                stop_event=run_stop,
                backoff=backoff,
            )
        except KeyboardInterrupt:
            pass
        finally:
            transport.close()
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
