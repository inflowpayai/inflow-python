import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Self, TypeVar
from urllib.parse import quote, urlsplit

import httpx

from .errors import InflowApiError
from .options import ClientOptions

_RETRY_STATUSES = {429, 502, 503, 504}
_SENSITIVE = {
    "authorization",
    "proxyauthorization",
    "cookie",
    "setcookie",
    "xapikey",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "privatekey",
    "secretkey",
    "password",
    "credential",
    "signature",
    "paymentsignature",
    "xpayment",
}
_T = TypeVar("_T")


def _sensitive(key: str) -> bool:
    return key.lower().replace("-", "").replace("_", "") in _SENSITIVE


def _redact(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _sensitive(key) else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def _text(body: Mapping[str, object], key: str, fallback: str) -> str:
    value = body.get(key)
    return value if isinstance(value, str) and value else fallback


def _error(path: str, response: httpx.Response, secrets: tuple[str, ...]) -> InflowApiError:
    body = _redact(_parse(response), secrets)
    code, message = "UNEXPECTED_ERROR", "request failed"
    if isinstance(body, dict):
        code = _text(body, "code", code)
        message = _text(body, "message", message)
        entries = body.get("errors")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            code = _text(entries[0], "code", code)
            message = _text(entries[0], "message", message)
        message = _text(body, "detail", message)
    headers = {
        key: str(_redact(value, secrets))
        for key, value in response.headers.items()
        if not _sensitive(key)
    }
    return InflowApiError(
        message,
        code=code,
        http_status=response.status_code,
        endpoint=path,
        body=body,
        headers=headers,
    )


def _parse(response: httpx.Response) -> object:
    if not response.content:
        return None
    try:
        return json.loads(response.content)
    except (ValueError, UnicodeDecodeError):
        return response.text


def _positive(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout and poll interval must be finite and positive")


def _credential(value: str) -> None:
    if not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError(
            "credentials must be nonempty ASCII without whitespace or control characters"
        )


class Client:
    def __init__(self, options: ClientOptions) -> None:
        if options.environment not in ("production", "sandbox"):
            raise ValueError("unknown InFlow environment")
        base = options.base_url or (
            "https://api.inflowpay.ai"
            if options.environment == "production"
            else "https://sandbox.inflowpay.ai"
        )
        parsed = urlsplit(base)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base
            or "#" in base
        ):
            raise ValueError(
                "base URL must be HTTP or HTTPS without credentials, query, or fragment"
            )
        if options.api_key is not None:
            _credential(options.api_key)
            if options.access_token is not None:
                raise ValueError("API key and access token provider are mutually exclusive")
        _positive(options.timeout)
        self.base_url = base.rstrip("/")
        self._options = options
        self._http = httpx.AsyncClient(
            transport=options.transport,
            follow_redirects=False,
            timeout=None,
        )

    async def __aenter__(self) -> Self:
        await self._http.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        headers: Mapping[str, str] | None = None,
        retries: int = 0,
    ) -> object:
        # Retry permission belongs to the protocol operation, not the HTTP method.
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ValueError("expected an InFlow API path beginning with one slash")
        if retries < 0:
            raise ValueError("retries must not be negative")
        extra = dict(headers or {})
        if any(key.lower() in ("authorization", "x-api-key", "cookie") for key in extra):
            raise ValueError("request headers must not override authentication")
        content = None if body is None else json.dumps(body, allow_nan=False).encode()
        attempt = 0
        while True:
            error = await self._attempt(method, path, content, extra)
            if not isinstance(error, InflowApiError):
                return error
            if attempt >= min(retries, 3) or (
                error.http_status not in _RETRY_STATUSES and error.http_status != 0
            ):
                raise error
            await asyncio.sleep(0.2 * 2**attempt * (1 + random.random() / 4))
            attempt += 1

    async def _attempt(
        self,
        method: str,
        path: str,
        content: bytes | None,
        extra: dict[str, str],
    ) -> object:
        await asyncio.sleep(0)
        token = self._options.api_key
        # Provider failures are application errors, not retryable transport failures.
        if self._options.access_token is not None:
            token = await self._options.access_token()
            _credential(token)
            # A provider can request cancellation without yielding before it returns.
            await asyncio.sleep(0)
        headers = httpx.Headers(extra)
        headers["Accept"] = "application/json"
        headers["User-Agent"] = "inflowpay (python)"
        if content is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            if self._options.api_key is not None:
                headers["X-API-KEY"] = token
            else:
                headers["Authorization"] = f"Bearer {token}"
        # A directly constructed Request does not inherit the HTTPX client's cookie jar.
        request = httpx.Request(method, self.base_url + path, content=content, headers=headers)
        try:
            async with asyncio.timeout(self._options.timeout):
                response = await self._http.send(request, follow_redirects=False)
                task = asyncio.current_task()
                assert task is not None
                if task.cancelling():
                    await asyncio.sleep(0)
        except (TimeoutError, httpx.TimeoutException):
            return InflowApiError(
                "request timed out",
                code="TIMEOUT",
                http_status=0,
                endpoint=path,
            )
        except (httpx.TransportError, httpx.DecodingError):
            return InflowApiError(
                "network request failed",
                code="NETWORK_ERROR",
                http_status=0,
                endpoint=path,
            )
        if response.is_success:
            return _parse(response)
        return _error(path, response, (token,) if token else ())

    async def get_payment_status(
        self, transaction_id: str, *, retries: int = 0
    ) -> dict[str, object]:
        value = await self.request(
            "GET", f"/v1/transactions/{quote(transaction_id, safe='')}", retries=retries
        )
        if not isinstance(value, dict):
            raise ValueError("Payment status response must be an object")
        return {str(key): item for key, item in value.items()}

    async def cancel_approval(self, approval_id: str) -> None:
        async def cancel() -> None:
            try:
                async with asyncio.timeout(5):
                    await self.request(
                        "POST", f"/v1/approvals/{quote(approval_id, safe='')}/cancel"
                    )
            except Exception:
                # Cleanup must not replace the payment flow's original failure.
                pass

        # Unlike Node fire-and-forget, await bounded cleanup before the application exits.
        task = asyncio.create_task(cancel())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


async def poll(read: Callable[[], Awaitable[tuple[_T, bool, float]]], *, timeout: float) -> _T:
    _positive(timeout)
    # Protocol code interprets states and chooses which read failures may be retried.
    async with asyncio.timeout(timeout):
        while True:
            value, done, interval = await read()
            if done:
                return value
            if not math.isfinite(interval) or interval < 0:
                raise ValueError("poll interval must be finite and nonnegative")
            await asyncio.sleep(interval)
