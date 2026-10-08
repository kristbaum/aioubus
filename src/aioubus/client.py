"""Async client for OpenWrt's ubus HTTP/JSON-RPC endpoint."""

import asyncio
import base64
import binascii
import itertools
import json
import logging
import ssl
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Literal, Self

import aiohttp
from yarl import URL

from ._parse import JsonObject, as_list, as_object
from .const import (
    DEFAULT_PATH,
    DEFAULT_TIMEOUT,
    NULL_SESSION_ID,
    JsonRpcError,
    UbusStatus,
)
from .exceptions import (
    UbusAuthenticationError,
    UbusConnectionError,
    UbusError,
    UbusHttpError,
    UbusPermissionError,
    UbusResponseError,
    UbusSSLError,
    UbusTimeoutError,
    error_for_rpc_code,
    error_for_status,
)
from .models import (
    BoardJson,
    DhcpLease,
    DnsmasqLease,
    HostapdClients,
    HostHint,
    NetworkDevice,
    OdhcpdLease,
    SessionInfo,
    SystemBoard,
    UciSection,
    UciValue,
    WirelessRadio,
    parse_dhcp_leases,
    parse_dnsmasq_leases,
    parse_host_hints,
    parse_odhcpd_leases,
    parse_uci_config,
    parse_uci_section,
    parse_uci_value,
)

_LOGGER = logging.getLogger(__name__)

# JSON-RPC errors uhttpd/nginx return when the session is unknown or lacks
# access. An expired or reboot-invalidated session looks exactly like this.
_SESSION_REJECTED = frozenset({JsonRpcError.ACCESS_DENIED, JsonRpcError.SESSION_NOT_FOUND})

# Renew proactively this many seconds before the session would expire.
_EXPIRY_MARGIN = 5.0

_HOSTAPD_PREFIX = "hostapd."
_DNSMASQ_DEFAULT_LEASEFILE = "/tmp/dhcp.leases"  # noqa: S108 - device path


class _SessionRejectedError(Exception):
    """Internal: the web server rejected the session (-32002/-32001)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_envelope(body: bytes, context: str) -> JsonObject:
    """Decode a JSON-RPC response; raise for ``error`` objects."""
    try:
        decoded = json.loads(body)
    except ValueError as err:
        raise UbusResponseError(f"{context}: response is not valid JSON") from err
    envelope = as_object(decoded, f"{context}: response")
    error = envelope.get("error")
    if error is not None:
        err_obj = as_object(error, f"{context}: error")
        code = err_obj.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise UbusResponseError(f"{context}: error object without integer code")
        message = err_obj.get("message")
        message = message if isinstance(message, str) else "error"
        if code in _SESSION_REJECTED:
            raise _SessionRejectedError(code, message)
        raise error_for_rpc_code(code, message, context)
    if "result" not in envelope:
        raise UbusResponseError(f"{context}: response has neither result nor error")
    return envelope


class UbusClient:
    """Client for the ubus JSON-RPC endpoint served by uhttpd or nginx.

    Pass an existing :class:`aiohttp.ClientSession` as ``session`` to share
    it; otherwise the client creates one and closes it in :meth:`close`.

    Login happens lazily on the first call, or explicitly via :meth:`login`.
    If the device rejects the session (expiry, reboot), the client logs in
    once more and retries; see :meth:`call`.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        session: aiohttp.ClientSession | None = None,
        scheme: Literal["http", "https"] = "http",
        port: int | None = None,
        path: str = DEFAULT_PATH,
        verify_ssl: bool = True,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session_timeout: int | None = None,
    ) -> None:
        """Create a client.

        :param host: Hostname or IP address (IPv6 without brackets).
        :param scheme: ``"http"`` or ``"https"``.
        :param port: TCP port; defaults to the scheme's default.
        :param path: Endpoint path, ``/ubus`` unless uhttpd's ``-u`` differs.
        :param verify_ssl: Verify the server certificate for HTTPS.
        :param ssl_context: Custom context (e.g. to trust a self-signed
            certificate); takes precedence over ``verify_ssl``.
        :param timeout: Per-request timeout in seconds.
        :param session_timeout: Requested rpcd session timeout in seconds;
            rpcd's default (300) when ``None``.
        """
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported scheme: {scheme!r}")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not path.startswith("/"):
            path = f"/{path}"
        self._url = URL.build(scheme=scheme, host=host, port=port, path=path)
        self._username = username
        self._password = password
        self._session = session
        self._owns_session = session is None
        self._ssl: ssl.SSLContext | bool = ssl_context if ssl_context is not None else verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session_timeout = session_timeout
        self._ids = itertools.count(1)
        self._login_lock = asyncio.Lock()
        self._token: str | None = None
        self._token_generation = 0
        self._idle_timeout = 0.0
        self._expires_at = 0.0
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={str(self._url)!r}, username={self._username!r})"

    @property
    def url(self) -> str:
        """The endpoint URL."""
        return str(self._url)

    @property
    def logged_in(self) -> bool:
        """Whether the client holds a session it believes to be valid."""
        return self._token is not None and time.monotonic() < self._expires_at

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Forget the session and close the HTTP session if we created it."""
        self._token = None
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
        self._closed = True

    # -- transport --------------------------------------------------------

    def _http(self) -> aiohttp.ClientSession:
        if self._closed and self._owns_session:
            raise UbusConnectionError("client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post(self, method: str, params: list[Any] | None, context: str) -> JsonObject:
        """Send one JSON-RPC request and return the decoded envelope."""
        request: JsonObject = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            request["params"] = params
        _LOGGER.debug("ubus request: %s", context)
        try:
            async with self._http().post(
                self._url,
                json=request,
                ssl=self._ssl,
                timeout=self._timeout,
            ) as resp:
                if resp.status >= 300:  # noqa: PLR2004
                    raise UbusHttpError(
                        f"{context}: HTTP {resp.status} from {self._url}",
                        http_status=resp.status,
                    )
                body = await resp.read()
        except TimeoutError as err:
            raise UbusTimeoutError(f"{context}: timed out talking to {self._url}") from err
        except aiohttp.ClientSSLError as err:
            raise UbusSSLError(f"{context}: TLS error talking to {self._url}: {err}") from err
        except aiohttp.ClientError as err:
            raise UbusConnectionError(f"{context}: cannot reach {self._url}: {err}") from err
        return _parse_envelope(body, context)

    async def _call_with_sid(
        self, sid: str, obj: str, procedure: str, args: Mapping[str, object]
    ) -> JsonObject | None:
        context = f"{obj}.{procedure}"
        envelope = await self._post("call", [sid, obj, procedure, dict(args)], context)
        result = as_list(envelope["result"], f"{context}: result")
        if not result:
            raise UbusResponseError(f"{context}: empty result array")
        status = result[0]
        if isinstance(status, bool) or not isinstance(status, int):
            raise UbusResponseError(f"{context}: non-integer status")
        if status != UbusStatus.OK:
            raise error_for_status(status, context)
        if len(result) < 2:  # noqa: PLR2004
            return None
        return as_object(result[1], f"{context}: payload")

    # -- session ----------------------------------------------------------

    async def login(self) -> SessionInfo:
        """Log in and store the new session, replacing any existing one.

        Raises :class:`UbusAuthenticationError` if the credentials are
        rejected or rpcd's ACL does not allow login.
        """
        async with self._login_lock:
            return await self._login_locked()

    async def renew_session(self) -> SessionInfo:
        """Obtain a fresh session (alias of :meth:`login`)."""
        return await self.login()

    async def _login_locked(self) -> SessionInfo:
        args: JsonObject = {"username": self._username, "password": self._password}
        if self._session_timeout is not None:
            args["timeout"] = self._session_timeout
        try:
            payload = await self._call_with_sid(NULL_SESSION_ID, "session", "login", args)
        except _SessionRejectedError as err:
            raise UbusAuthenticationError(
                "login is not permitted for unauthenticated sessions "
                "(check rpcd's 'unauthenticated' ACL)",
                rpc_code=err.code,
            ) from None
        except UbusPermissionError as err:
            raise UbusAuthenticationError(
                "login rejected: invalid username or password", status=err.status
            ) from None
        if payload is None:
            raise UbusResponseError("session.login: missing payload")
        token = payload.get("ubus_rpc_session")
        if not isinstance(token, str) or not token:
            raise UbusResponseError("session.login: missing ubus_rpc_session")
        info = SessionInfo.from_json(payload)
        self._token = token
        self._token_generation += 1
        self._idle_timeout = float(info.timeout)
        self._expires_at = time.monotonic() + info.expires
        _LOGGER.debug("ubus login succeeded, session timeout %ss", info.timeout)
        return info

    def _current_token(self) -> tuple[str, int]:
        if self._token is None:
            raise UbusAuthenticationError("no session")
        return self._token, self._token_generation

    def _session_lapsed(self) -> bool:
        margin = min(_EXPIRY_MARGIN, self._idle_timeout / 4)
        return time.monotonic() >= self._expires_at - margin

    async def _ensure_token(self) -> tuple[str, int]:
        """Return a usable token and its generation, logging in if needed."""
        async with self._login_lock:
            if self._token is None or self._session_lapsed():
                await self._login_locked()
            return self._current_token()

    async def _relogin_after_reject(self, generation: int) -> tuple[str, int]:
        """Re-login unless another task already did since ``generation``."""
        async with self._login_lock:
            if self._token is None or self._token_generation == generation:
                _LOGGER.debug("ubus session rejected; logging in again")
                await self._login_locked()
            return self._current_token()

    def _touch(self, generation: int) -> None:
        # A successful authorized call resets rpcd's idle timer.
        if generation == self._token_generation:
            self._expires_at = time.monotonic() + self._idle_timeout

    # -- generic API ------------------------------------------------------

    async def call(
        self,
        obj: str,
        procedure: str,
        args: Mapping[str, object] | None = None,
        /,
        **kwargs: object,
    ) -> JsonObject | None:
        """Call ``obj.procedure`` and return its raw payload.

        Arguments may be given as a mapping, as keywords, or both (keywords
        win). Returns ``None`` when the procedure returns no data.

        If the web server rejects the session (JSON-RPC ``-32002``/``-32001``,
        which is how an expired or reboot-invalidated session presents), the
        client logs in once and retries once. If the retry is rejected too,
        :class:`UbusPermissionError` is raised; if the re-login fails,
        :class:`UbusAuthenticationError`. A ubus ``PERMISSION_DENIED`` status
        returned by the procedure itself is not retried: the web server has
        already validated the session at that point.
        """
        merged: dict[str, object] = {**(args or {}), **kwargs}
        token, generation = await self._ensure_token()
        try:
            payload = await self._call_with_sid(token, obj, procedure, merged)
        except _SessionRejectedError:
            token, generation = await self._relogin_after_reject(generation)
            try:
                payload = await self._call_with_sid(token, obj, procedure, merged)
            except _SessionRejectedError as err:
                raise UbusPermissionError(
                    f"{obj}.{procedure}: access denied for user {self._username!r}",
                    rpc_code=err.code,
                ) from None
        self._touch(generation)
        return payload

    async def list_objects(self, *patterns: str) -> dict[str, dict[str, dict[str, str]]]:
        """List objects matching ``patterns`` with their method signatures.

        Patterns support a trailing ``*`` wildcard, e.g. ``"hostapd.*"``.
        The result maps object name -> method name -> argument name -> type.
        Listing does not require a session.

        Only uhttpd returns object names with signatures. nginx-mod-ubus
        returns an array of nameless signature tables, which raises
        :class:`UbusResponseError`; use :meth:`list_object_names` there.
        """
        if not patterns:
            patterns = ("*",)
        envelope = await self._post("list", list(patterns), "list")
        result = envelope["result"]
        if isinstance(result, list):
            if not result:
                return {}
            raise UbusResponseError(
                "list: server returned signatures without object names "
                "(nginx-mod-ubus); use list_object_names()"
            )
        objects = as_object(result, "list: result")
        listing: dict[str, dict[str, dict[str, str]]] = {}
        for name, methods in objects.items():
            methods_obj = as_object(methods, f"list: {name}")
            listing[name] = {}
            for method, signature in methods_obj.items():
                sig = as_object(signature, f"list: {name}.{method}")
                if not all(isinstance(t, str) for t in sig.values()):
                    raise UbusResponseError(f"list: {name}.{method}: non-string type")
                listing[name][method] = dict(sig)
        return listing

    async def list_object_names(self) -> tuple[str, ...]:
        """Return the names of all ubus objects. Does not require a session."""
        envelope = await self._post("list", None, "list")
        result = envelope["result"]
        # nginx-mod-ubus wraps the name array in a one-element array.
        if isinstance(result, list) and len(result) == 1 and isinstance(result[0], list):
            result = result[0]
        names = as_list(result, "list: result")
        if not all(isinstance(name, str) for name in names):
            raise UbusResponseError("list: expected array of object names")
        return tuple(names)

    # -- luci-rpc (rpcd-mod-luci) -------------------------------------------

    async def get_host_hints(self) -> dict[str, HostHint]:
        """``luci-rpc getHostHints``: all known hosts keyed by MAC.

        Requires ``rpcd-mod-luci``.
        """
        return parse_host_hints(await self.call("luci-rpc", "getHostHints"))

    async def get_dhcp_leases(self, family: Literal[0, 4, 6] = 0) -> tuple[DhcpLease, ...]:
        """``luci-rpc getDHCPLeases``; ``family`` 0 returns IPv4 and IPv6.

        Requires ``rpcd-mod-luci``.
        """
        args = {"family": family} if family else {}
        return parse_dhcp_leases(await self.call("luci-rpc", "getDHCPLeases", args))

    async def get_wireless_devices(self) -> dict[str, WirelessRadio]:
        """``luci-rpc getWirelessDevices``: radios keyed by name.

        Raises :class:`UbusNotFoundError` on devices without wireless.
        Requires ``rpcd-mod-luci``.
        """
        payload = await self.call("luci-rpc", "getWirelessDevices") or {}
        return {name: WirelessRadio.from_json(name, data) for name, data in payload.items()}

    async def get_network_devices(self) -> dict[str, NetworkDevice]:
        """``luci-rpc getNetworkDevices``: kernel network devices by name.

        Requires ``rpcd-mod-luci``.
        """
        payload = await self.call("luci-rpc", "getNetworkDevices") or {}
        return {name: NetworkDevice.from_json(name, data) for name, data in payload.items()}

    async def get_board_json(self) -> BoardJson:
        """``luci-rpc getBoardJSON``: contents of ``/etc/board.json``.

        Requires ``rpcd-mod-luci``.
        """
        return BoardJson.from_json(await self.call("luci-rpc", "getBoardJSON"))

    # -- procd / rpcd / odhcpd -------------------------------------------

    async def get_system_board(self) -> SystemBoard:
        """``system board``: model, hostname and firmware release."""
        return SystemBoard.from_json(await self.call("system", "board"))

    async def list_hostapd_interfaces(self) -> tuple[str, ...]:
        """Interfaces that have a ``hostapd.<iface>`` object, e.g. ``phy0-ap0``.

        Includes wired 802.1X authenticator objects, which hostapd also
        registers as ``hostapd.<iface>``. Does not require a session.
        """
        names = await self.list_object_names()
        return tuple(
            sorted(
                name.removeprefix(_HOSTAPD_PREFIX)
                for name in names
                if name.startswith(_HOSTAPD_PREFIX)
            )
        )

    async def get_hostapd_clients(self, interface: str) -> HostapdClients:
        """``hostapd.<iface> get_clients`` for one interface.

        ``interface`` may be given with or without the ``hostapd.`` prefix.
        """
        interface = interface.removeprefix(_HOSTAPD_PREFIX)
        payload = await self.call(f"{_HOSTAPD_PREFIX}{interface}", "get_clients")
        return HostapdClients.from_json(interface, payload)

    async def uci_get_config(
        self, config: str, *, section_type: str | None = None
    ) -> dict[str, UciSection]:
        """``uci get`` for a whole config, optionally filtered by section type."""
        args: JsonObject = {"config": config}
        if section_type is not None:
            args["type"] = section_type
        return parse_uci_config(await self.call("uci", "get", args), f"uci.get({config})")

    async def uci_get_section(self, config: str, section: str) -> UciSection:
        """``uci get`` for one section."""
        payload = await self.call("uci", "get", {"config": config, "section": section})
        return parse_uci_section(payload, f"uci.get({config}.{section})")

    async def uci_get_option(self, config: str, section: str, option: str) -> UciValue:
        """``uci get`` for one option: ``str``, or ``tuple[str, ...]`` for lists."""
        payload = await self.call(
            "uci", "get", {"config": config, "section": section, "option": option}
        )
        return parse_uci_value(payload, f"uci.get({config}.{section}.{option})")

    async def file_read(self, path: str) -> str:
        """``file read``: a text file's contents. Requires ``rpcd-mod-file``."""
        payload = await self.call("file", "read", {"path": path})
        data = (payload or {}).get("data")
        if not isinstance(data, str):
            raise UbusResponseError("file.read: missing 'data'")
        return data

    async def file_read_bytes(self, path: str) -> bytes:
        """``file read`` with base64 transfer, for binary files."""
        payload = await self.call("file", "read", {"path": path, "base64": True})
        data = (payload or {}).get("data")
        if not isinstance(data, str):
            raise UbusResponseError("file.read: missing 'data'")
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as err:
            raise UbusResponseError("file.read: invalid base64 data") from err

    async def get_dnsmasq_leases(self, path: str | None = None) -> tuple[DnsmasqLease, ...]:
        """Read and parse dnsmasq's lease file.

        Without ``path``, the ``leasefile`` option of the first ``dnsmasq``
        section in ``/etc/config/dhcp`` is used, falling back to dnsmasq's
        OpenWrt default ``/tmp/dhcp.leases``.
        """
        if path is None:
            sections = await self.uci_get_config("dhcp", section_type="dnsmasq")
            leasefile = next(iter(sections.values())).options.get("leasefile") if sections else None
            path = leasefile if isinstance(leasefile, str) else _DNSMASQ_DEFAULT_LEASEFILE
        return parse_dnsmasq_leases(await self.file_read(path))

    async def get_odhcpd_ipv4_leases(self) -> tuple[OdhcpdLease, ...]:
        """Return odhcpd's ``dhcp ipv4leases``.

        Requires the full ``odhcpd`` package (not ``odhcpd-ipv6only``) and
        odhcpd serving DHCPv4; otherwise :class:`UbusNotFoundError`.
        """
        return parse_odhcpd_leases(await self.call("dhcp", "ipv4leases"))


__all__ = ["UbusClient", "UbusError"]
