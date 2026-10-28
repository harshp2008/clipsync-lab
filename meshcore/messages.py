"""Message envelope and Hello structures for meshcore."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Hello:
    """Hello announcement message payload."""

    device_id: str
    name: str
    perm: int | None = None
    temp_rank: int | None = None
    will_hub: bool = True


def encode_hello(hello: Hello) -> dict[str, Any]:
    """Encode a Hello message into a JSON-serializable dictionary.

    device_id and name are always included.
    perm and temp_rank are only included when not None.
    will_hub is always included.
    """
    data: dict[str, Any] = {
        "device_id": hello.device_id,
        "name": hello.name,
        "will_hub": hello.will_hub,
    }
    if hello.perm is not None:
        data["perm"] = hello.perm
    if hello.temp_rank is not None:
        data["temp_rank"] = hello.temp_rank
    return data


def decode_hello(data: Mapping[str, Any]) -> Hello:
    """Decode a mapping into a Hello message.

    Absent perm or temp_rank defaults to None.
    Absent will_hub defaults to True.
    """
    perm = data.get("perm")
    temp_rank = data.get("temp_rank")
    will_hub = data.get("will_hub")
    if will_hub is None:
        will_hub_val = True
    else:
        will_hub_val = bool(will_hub)

    return Hello(
        device_id=str(data["device_id"]),
        name=str(data["name"]),
        perm=int(perm) if perm is not None else None,
        temp_rank=int(temp_rank) if temp_rank is not None else None,
        will_hub=will_hub_val,
    )
