"""Defensive accessors for decoded JSON.

Every helper raises :class:`UbusResponseError` on a shape mismatch, so a
truncated or unexpected payload never surfaces as ``TypeError``/``KeyError``.
"""

import re
from collections.abc import Mapping
from typing import Any

from .exceptions import UbusResponseError

type JsonObject = dict[str, Any]

_HEX_RE = re.compile(r"[0-9a-fA-F]{12}")


def normalize_mac(value: str) -> str:
    """Normalize a MAC address to lowercase, colon-separated form.

    Accepts ``aa:bb:cc:dd:ee:ff``, ``AA-BB-CC-DD-EE-FF``, ``aabb.ccdd.eeff``
    and unseparated ``aabbccddeeff`` (as returned by odhcpd).
    Raises :class:`ValueError` if ``value`` is not a 48-bit MAC address.
    """
    digits = value.strip().replace(":", "").replace("-", "").replace(".", "")
    if not _HEX_RE.fullmatch(digits):
        raise ValueError(f"not a MAC address: {value!r}")
    digits = digits.lower()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def parse_mac(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise UbusResponseError(f"{where}: expected MAC string, got {type(value).__name__}")
    try:
        return normalize_mac(value)
    except ValueError as err:
        raise UbusResponseError(f"{where}: {err}") from None


def as_object(value: object, where: str) -> JsonObject:
    if not isinstance(value, dict):
        raise UbusResponseError(f"{where}: expected object, got {type(value).__name__}")
    return value


def as_list(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise UbusResponseError(f"{where}: expected array, got {type(value).__name__}")
    return value


def get_str(obj: Mapping[str, Any], key: str, where: str) -> str:
    value: object = obj.get(key)
    if not isinstance(value, str):
        raise UbusResponseError(f"{where}: missing or non-string {key!r}")
    return value


def opt_str(obj: Mapping[str, Any], key: str, where: str) -> str | None:
    value: object = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise UbusResponseError(f"{where}: non-string {key!r}")
    return value


def opt_int(obj: Mapping[str, Any], key: str, where: str) -> int | None:
    value: object = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UbusResponseError(f"{where}: non-integer {key!r}")
    return value


def get_int(obj: Mapping[str, Any], key: str, where: str) -> int:
    value = opt_int(obj, key, where)
    if value is None:
        raise UbusResponseError(f"{where}: missing {key!r}")
    return value


def opt_bool(obj: Mapping[str, Any], key: str, where: str) -> bool | None:
    """Read a blobmsg boolean.

    blobmsg ``u8`` values are emitted as JSON booleans, but tolerate 0/1.
    """
    value: object = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise UbusResponseError(f"{where}: non-boolean {key!r}")


def str_tuple(obj: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    """Read an optional array of strings; absent means empty."""
    value: object = obj.get(key)
    if value is None:
        return ()
    items = as_list(value, f"{where}.{key}")
    if not all(isinstance(item, str) for item in items):
        raise UbusResponseError(f"{where}.{key}: expected array of strings")
    return tuple(items)
