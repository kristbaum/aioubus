"""Protocol constants for ubus over HTTP (uhttpd-mod-ubus / nginx-mod-ubus)."""

from enum import IntEnum
from typing import Final

#: Session ID used for unauthenticated requests (``session.login``).
NULL_SESSION_ID: Final = "00000000000000000000000000000000"

DEFAULT_PATH: Final = "/ubus"
DEFAULT_TIMEOUT: Final = 10.0

#: rpcd's default session timeout in seconds (``RPC_DEFAULT_SESSION_TIMEOUT``).
DEFAULT_SESSION_TIMEOUT: Final = 300


class UbusStatus(IntEnum):
    """``enum ubus_msg_status`` from ``ubusmsg.h`` in openwrt/ubus."""

    OK = 0
    INVALID_COMMAND = 1
    INVALID_ARGUMENT = 2
    METHOD_NOT_FOUND = 3
    NOT_FOUND = 4
    NO_DATA = 5
    PERMISSION_DENIED = 6
    TIMEOUT = 7
    NOT_SUPPORTED = 8
    UNKNOWN_ERROR = 9
    CONNECTION_FAILED = 10
    NO_MEMORY = 11
    PARSE_ERROR = 12
    SYSTEM_ERROR = 13


class JsonRpcError(IntEnum):
    """JSON-RPC error codes emitted by uhttpd's and nginx's ubus handlers.

    These are produced by the web server itself *before* the ubus procedure
    runs, and are reported as a JSON-RPC ``error`` object rather than a
    ``[status, payload]`` result.
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    OBJECT_NOT_FOUND = -32000
    SESSION_NOT_FOUND = -32001
    ACCESS_DENIED = -32002
    TIMEOUT = -32003
