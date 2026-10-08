"""Exception hierarchy for aioubus.

Every error raised by the public API derives from :class:`UbusError`.
Errors from ``aiohttp`` and ``json`` are always wrapped.
"""

from .const import JsonRpcError, UbusStatus


class UbusError(Exception):
    """Base class for all aioubus errors.

    ``status`` is the ubus status code when the error came from a
    ``[status, ...]`` result, and ``rpc_code`` the JSON-RPC error code when it
    came from the web server's ``error`` object. Either may be ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        rpc_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.rpc_code = rpc_code


class UbusConnectionError(UbusError):
    """The device could not be reached or the HTTP exchange failed."""


class UbusTimeoutError(UbusConnectionError):
    """The request timed out, either locally or on the device."""


class UbusSSLError(UbusConnectionError):
    """TLS handshake or certificate verification failed."""


class UbusHttpError(UbusConnectionError):
    """The web server answered with a non-2xx HTTP status."""

    def __init__(self, message: str, *, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


class UbusAuthenticationError(UbusError):
    """Login was rejected (wrong credentials, or login not permitted)."""


class UbusPermissionError(UbusError):
    """The session is valid but not allowed to perform this call."""


class UbusResponseError(UbusError):
    """The response was malformed or did not have the expected shape."""


class UbusCallError(UbusError):
    """A ubus procedure returned a non-zero status."""


class UbusInvalidArgumentError(UbusCallError):
    """``INVALID_COMMAND``/``INVALID_ARGUMENT``, or invalid JSON-RPC params."""


class UbusNotFoundError(UbusCallError):
    """The requested object, procedure or resource does not exist."""


class UbusObjectNotFoundError(UbusNotFoundError):
    """The ubus object does not exist (e.g. the providing package is absent)."""


class UbusMethodNotFoundError(UbusNotFoundError):
    """The ubus object exists but has no such procedure."""


class UbusNoDataError(UbusCallError):
    """The procedure had no data to return (``NO_DATA``)."""


class UbusNotSupportedError(UbusCallError):
    """The procedure does not support the request (``NOT_SUPPORTED``)."""


_STATUS_ERRORS: dict[int, type[UbusError]] = {
    UbusStatus.INVALID_COMMAND: UbusInvalidArgumentError,
    UbusStatus.INVALID_ARGUMENT: UbusInvalidArgumentError,
    UbusStatus.METHOD_NOT_FOUND: UbusMethodNotFoundError,
    UbusStatus.NOT_FOUND: UbusNotFoundError,
    UbusStatus.NO_DATA: UbusNoDataError,
    UbusStatus.PERMISSION_DENIED: UbusPermissionError,
    UbusStatus.TIMEOUT: UbusTimeoutError,
    UbusStatus.NOT_SUPPORTED: UbusNotSupportedError,
}

_RPC_ERRORS: dict[int, type[UbusError]] = {
    JsonRpcError.INVALID_PARAMS: UbusInvalidArgumentError,
    JsonRpcError.OBJECT_NOT_FOUND: UbusObjectNotFoundError,
    JsonRpcError.SESSION_NOT_FOUND: UbusPermissionError,
    JsonRpcError.ACCESS_DENIED: UbusPermissionError,
    JsonRpcError.TIMEOUT: UbusTimeoutError,
}


def error_for_status(status: int, context: str) -> UbusError:
    """Build the exception for a non-zero ubus status."""
    try:
        name = UbusStatus(status).name
    except ValueError:
        name = "UNKNOWN_STATUS"
    cls = _STATUS_ERRORS.get(status, UbusCallError)
    return cls(f"{context} failed: {name} ({status})", status=status)


def error_for_rpc_code(code: int, message: str, context: str) -> UbusError:
    """Build the exception for a JSON-RPC error object."""
    cls = _RPC_ERRORS.get(code, UbusCallError)
    return cls(f"{context} failed: {message} ({code})", rpc_code=code)
