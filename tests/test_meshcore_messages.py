"""Unit tests for meshcore.messages edge cases and behaviors."""

from __future__ import annotations

import dataclasses
import json
from types import MappingProxyType
import pytest

from meshcore.messages import Hello, decode_hello, encode_hello


def test_hello_dataclass_properties() -> None:
    hello = Hello("dev-id", "my-device")
    assert hello.device_id == "dev-id"
    assert hello.name == "my-device"
    assert hello.perm is None
    assert hello.temp_rank is None
    assert hello.will_hub is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        hello.name = "mutated"  # type: ignore[misc]


def test_encode_hello_json_serializability() -> None:
    h1 = Hello("d1", "Name1", perm=0, temp_rank=1, will_hub=False)
    encoded1 = encode_hello(h1)
    raw_json = json.dumps(encoded1)
    parsed = json.loads(raw_json)
    assert parsed == {
        "device_id": "d1",
        "name": "Name1",
        "perm": 0,
        "temp_rank": 1,
        "will_hub": False,
    }

    h_minimal = Hello("d2", "Name2")
    encoded_minimal = encode_hello(h_minimal)
    assert "perm" not in encoded_minimal
    assert "temp_rank" not in encoded_minimal
    assert encoded_minimal["will_hub"] is True
    assert json.loads(json.dumps(encoded_minimal)) == encoded_minimal


def test_decode_hello_with_various_mappings() -> None:
    # Using MappingProxyType to test generic Mapping support
    raw_dict = {
        "device_id": "d-proxy",
        "name": "Proxy Device",
        "perm": 10,
        "temp_rank": 20,
        "will_hub": False,
        "extra_field": "ignored",
    }
    proxy = MappingProxyType(raw_dict)
    decoded = decode_hello(proxy)
    assert decoded == Hello("d-proxy", "Proxy Device", perm=10, temp_rank=20, will_hub=False)


def test_decode_hello_default_fallbacks() -> None:
    # Missing will_hub, perm, temp_rank
    d1 = decode_hello({"device_id": "dev", "name": "Test"})
    assert d1.perm is None
    assert d1.temp_rank is None
    assert d1.will_hub is True

    # Explicit None values in dict
    d2 = decode_hello(
        {"device_id": "dev", "name": "Test", "perm": None, "temp_rank": None, "will_hub": None}
    )
    assert d2.perm is None
    assert d2.temp_rank is None
    assert d2.will_hub is True

    # Numeric or truthy will_hub
    d3 = decode_hello({"device_id": "dev", "name": "Test", "will_hub": 0})
    assert d3.will_hub is False

    d4 = decode_hello({"device_id": "dev", "name": "Test", "will_hub": 1})
    assert d4.will_hub is True
