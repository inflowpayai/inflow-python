import asyncio
from collections.abc import AsyncIterator
from typing import Literal, cast

import httpx
import pytest

from inflowpay import ClientOptions, InflowApiError
from inflowpay._runtime import Client, _redact, poll


@pytest.mark.parametrize(
    "environment,expected",
    [
        ("production", "https://api.inflowpay.ai"),
        ("sandbox", "https://sandbox.inflowpay.ai"),
    ],
)
async def test_environment(environment: Literal["production", "sandbox"], expected: str) -> None:
    async with Client(ClientOptions(environment=environment)) as client:
        assert client.base_url == expected


def test_bad_environment() -> None:
    # Exercise invalid untyped consumer input at the public configuration boundary.
    with pytest.raises(ValueError):
        Client(ClientOptions(environment=cast("Literal['production', 'sandbox']", "other")))


@pytest.mark.parametrize(
    "base",
    [
        "file:///tmp/a",
        "https:///missing",
        "https://user:pass@example.com",
        "https://x/?",
        "https://x/#",
        "https://:pass@x",
    ],
)
def test_bad_base(base: str) -> None:
    with pytest.raises(ValueError):
        Client(ClientOptions(base_url=base))


@pytest.mark.parametrize("key", ["", "a b", "a\nb", "é"])
def test_bad_key(key: str) -> None:
    with pytest.raises(ValueError):
        Client(ClientOptions(api_key=key))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_bad_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        Client(ClientOptions(timeout=timeout))


async def test_auth_and_ownership() -> None:
    requests: list[httpx.Request] = []

    class Transport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "session=secret"})

        async def aclose(self) -> None:
            self.closed = True

    transport = Transport()
    headers = {"x-custom": "value"}
    body = {"items": [1]}
    async with Client(
        ClientOptions(api_key="test-key", base_url="https://local.test/api/", transport=transport)
    ) as client:
        assert await client.request("POST", "/test", body=body, headers=headers) == {"ok": True}
        await client.request("GET", "/test")
    assert transport.closed
    assert str(requests[0].url) == "https://local.test/api/test"
    assert requests[0].headers["x-api-key"] == "test-key"
    assert requests[0].content == b'{"items": [1]}'
    assert "cookie" not in requests[1].headers
    assert requests[0].extensions["timeout"] == {
        "connect": None,
        "read": None,
        "write": None,
        "pool": None,
    }
    assert headers == {"x-custom": "value"} and body == {"items": [1]}
    await client.aclose()


async def test_anonymous_and_redirect() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.test"})

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        with pytest.raises(InflowApiError) as failure:
            await client.request("GET", "/test")
    assert len(seen) == 1 and "authorization" not in seen[0].headers
    assert "x-api-key" not in seen[0].headers
    assert failure.value.http_status == 302 and failure.value.body is None


async def test_bearer_retry_and_provider_error() -> None:
    tokens: list[str] = []

    async def token() -> str:
        return f"token-{len(tokens)}"

    def handle(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        return httpx.Response(503 if len(tokens) == 1 else 200, json=[1])

    async with Client(
        ClientOptions(access_token=token, transport=httpx.MockTransport(handle))
    ) as client:
        assert await client.request("GET", "/test", retries=1) == [1]
    assert tokens == ["Bearer token-0", "Bearer token-1"]
    error = RuntimeError("provider failed")

    async def fail() -> str:
        raise error

    async with Client(ClientOptions(access_token=fail)) as client:
        with pytest.raises(RuntimeError) as failure:
            await client.request("GET", "/test", retries=3)
    assert failure.value is error
    with pytest.raises(ValueError, match="mutually exclusive"):
        Client(ClientOptions(api_key="key", access_token=token))


async def test_invalid_token() -> None:
    async def token() -> str:
        return ""

    async with Client(ClientOptions(access_token=token)) as client:
        with pytest.raises(ValueError):
            await client.request("GET", "/test")


@pytest.mark.parametrize("path", ["https://evil.test", "//evil.test", "/x#fragment"])
async def test_bad_path(path: str) -> None:
    async with Client(ClientOptions()) as client:
        with pytest.raises(ValueError):
            await client.request("GET", path)


async def test_bad_request() -> None:
    async with Client(ClientOptions()) as client:
        with pytest.raises(ValueError):
            await client.request("GET", "/x", retries=-1)
        for header in ("Authorization", "X-API-KEY", "COOKIE"):
            with pytest.raises(ValueError):
                await client.request("GET", "/x", headers={header: "secret"})
        with pytest.raises(ValueError):
            await client.request("POST", "/x", body=float("nan"))


@pytest.mark.parametrize("status", [429, 502, 503, 504, 500, 401, 403])
async def test_retry_statuses(status: int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        with pytest.raises(InflowApiError):
            await client.request("POST", "/x")
        assert calls == 1
        calls = 0
        with pytest.raises(InflowApiError):
            await client.request("GET", "/x", retries=1)
        assert calls == (2 if status in (429, 502, 503, 504) else 1)


@pytest.mark.parametrize("kind", ["timeout", "httpx-timeout", "network"])
async def test_transport_errors(kind: str) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            await asyncio.sleep(1)
        if kind == "httpx-timeout":
            raise httpx.ReadTimeout("SECRET")
        raise httpx.ConnectError("SECRET")

    async with Client(
        ClientOptions(timeout=0.001, transport=httpx.MockTransport(handle))
    ) as client:
        with pytest.raises(InflowApiError) as failure:
            await client.request("GET", "/x")
    assert failure.value.code == ("NETWORK_ERROR" if kind == "network" else "TIMEOUT")
    assert failure.value.http_status == 0 and "SECRET" not in str(failure.value)


@pytest.mark.parametrize(
    "body,code,message",
    [
        ({"errors": [{"code": "DENIED", "message": "Wrong account"}]}, "DENIED", "Wrong account"),
        ({"code": "TOP", "message": "top", "detail": "detail"}, "TOP", "detail"),
        ({"errors": [None], "code": "", "message": 1}, "UNEXPECTED_ERROR", "request failed"),
        ({"errors": []}, "UNEXPECTED_ERROR", "request failed"),
        ({"errors": "bad"}, "UNEXPECTED_ERROR", "request failed"),
        ("not json", "UNEXPECTED_ERROR", "request failed"),
    ],
)
async def test_error_shapes(body: object, code: str, message: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=body)

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        with pytest.raises(InflowApiError) as failure:
            await client.request("GET", "/x")
    assert failure.value.code == code and str(failure.value) == message


async def test_redaction() -> None:
    body = {
        "message": "test-secret denied",
        "nested": [{"access_token": "other", "x": "test-secret"}],
        "n": 1,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json=body,
            headers={"x-request-id": "id", "x-info": "test-secret", "authorization": "other"},
        )

    async with Client(
        ClientOptions(api_key="test-secret", transport=httpx.MockTransport(handle))
    ) as client:
        with pytest.raises(InflowApiError) as failure:
            await client.request("GET", "/x")
    assert str(failure.value) == "[REDACTED] denied"
    assert failure.value.request_id == "id"
    assert failure.value.headers["x-info"] == "[REDACTED]"
    assert "authorization" not in failure.value.headers
    assert failure.value.body == {
        "message": "[REDACTED] denied",
        "nested": [{"access_token": "[REDACTED]", "x": "[REDACTED]"}],
        "n": 1,
    }
    assert body["message"] == "test-secret denied"
    assert _redact("unchanged", ()) == "unchanged"


@pytest.mark.parametrize("content,expected", [(b"", None), (b"bad{", "bad{"), (b"true", True)])
async def test_success_bodies(content: bytes, expected: object) -> None:
    async with Client(
        ClientOptions(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    ) as client:
        assert await client.request("GET", "/x") == expected


async def test_body_timeout_closes_stream() -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"a"
            await asyncio.sleep(1)

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()
    async with Client(
        ClientOptions(
            timeout=0.001,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        )
    ) as client:
        with pytest.raises(InflowApiError):
            await client.request("GET", "/x")
    assert stream.closed


async def test_cancel_request() -> None:
    started = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200)

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        task = asyncio.create_task(client.request("GET", "/x"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("status", [204, 404, 500])
async def test_cleanup(status: int) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        await client.cancel_approval("abc")
    assert len(seen) == 1 and seen[0].url.path == "/v1/approvals/abc/cancel"


async def test_cleanup_survives_caller_cancellation() -> None:
    started, finish = asyncio.Event(), asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        await finish.wait()
        return httpx.Response(204)

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        task = asyncio.create_task(client.cancel_approval("abc"))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_poll() -> None:
    count = 0

    async def read() -> tuple[int, bool, float]:
        nonlocal count
        count += 1
        return count, count == 2, 0.001

    assert await poll(read, timeout=1) == 2

    async def pending() -> tuple[int, bool, float]:
        return 1, False, 10

    with pytest.raises(TimeoutError):
        await poll(pending, timeout=0.001)

    async def invalid() -> tuple[int, bool, float]:
        return 1, False, -1

    with pytest.raises(ValueError):
        await poll(invalid, timeout=1)


async def test_zero_delay_poll_yields() -> None:
    reads = 0
    yielded = asyncio.Event()

    async def read() -> tuple[str, bool, float]:
        nonlocal reads
        reads += 1
        if reads == 1:
            asyncio.get_running_loop().call_soon(yielded.set)
            return "pending", False, 0
        assert yielded.is_set()
        return "ready", True, 0

    assert await poll(read, timeout=1) == "ready"
    assert reads == 2


@pytest.mark.parametrize("interval", [float("nan"), float("inf")])
async def test_nonfinite_poll_interval(interval: float) -> None:
    async def read() -> tuple[int, bool, float]:
        return 1, False, interval

    with pytest.raises(ValueError):
        await poll(read, timeout=1)


async def test_cleanup_deadline() -> None:
    stopped = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(60)
            return httpx.Response(204)
        finally:
            stopped.set()

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        async with asyncio.timeout(7):
            await client.cancel_approval("abc")
        assert stopped.is_set()


async def test_retry_limit() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        with pytest.raises(InflowApiError):
            await client.request("GET", "/x", retries=20)
    assert calls == 4


@pytest.mark.parametrize("failure", ["status", "network"])
async def test_cancel_during_backoff(failure: str) -> None:
    called = asyncio.Event()
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        called.set()
        if failure == "network":
            raise httpx.ReadError("response lost")
        return httpx.Response(503)

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:
        task = asyncio.create_task(client.request("GET", "/x", retries=3))
        await called.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 1


async def test_concurrent_credentials() -> None:
    issued = 0

    async def token() -> str:
        nonlocal issued
        issued += 1
        value = f"key-{issued}"
        await asyncio.sleep(0)
        return value

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=request.headers["authorization"])

    async with Client(
        ClientOptions(access_token=token, transport=httpx.MockTransport(handle))
    ) as client:
        results = await asyncio.gather(*(client.request("GET", "/x") for _ in range(10)))
    assert set(results) == {f"Bearer key-{number}" for number in range(1, 11)}


async def test_poll_read_deadline_and_failure() -> None:
    async def slow() -> tuple[int, bool, float]:
        await asyncio.sleep(10)
        return 1, True, 1

    with pytest.raises(TimeoutError):
        await poll(slow, timeout=0.001)
    error = RuntimeError("read failed")

    async def fail() -> tuple[int, bool, float]:
        raise error

    with pytest.raises(RuntimeError) as failure:
        await poll(fail, timeout=1)
    assert failure.value is error


async def test_poll_and_provider_cancellation() -> None:
    started = asyncio.Event()

    async def token() -> str:
        started.set()
        await asyncio.sleep(60)
        return "token"

    async with Client(ClientOptions(access_token=token)) as client:
        task = asyncio.create_task(client.request("GET", "/x"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    started.clear()

    async def read() -> tuple[int, bool, float]:
        started.set()
        return 1, False, 60

    polling = asyncio.create_task(poll(read, timeout=120))
    await started.wait()
    polling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await polling


async def test_cleanup_does_not_replace_original_failure() -> None:
    error = ValueError("original")
    async with Client(
        ClientOptions(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    ) as client:
        with pytest.raises(ValueError) as failure:
            try:
                raise error
            except ValueError:
                await client.cancel_approval("abc")
                raise
    assert failure.value is error


@pytest.mark.parametrize("phase", ["before-provider", "after-provider"])
async def test_cancellation_prevents_authenticated_request(phase: str) -> None:
    calls: list[str] = []

    async def token() -> str:
        calls.append("token")
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return "test-key"

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append("request")
        return httpx.Response(200, json={})

    async with Client(
        ClientOptions(access_token=token, transport=httpx.MockTransport(handle))
    ) as client:

        async def request() -> object:
            if phase == "before-provider":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
            return await client.request("POST", "/v1/transactions/mpp")

        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(request())
    assert calls == ([] if phase == "before-provider" else ["token"])


async def test_custom_transport_cancellation_does_not_return_success() -> None:
    completed: list[object] = []

    def handle(request: httpx.Request) -> httpx.Response:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return httpx.Response(200, json={"ok": True})

    async with Client(ClientOptions(transport=httpx.MockTransport(handle))) as client:

        async def request() -> None:
            completed.append(await client.request("GET", "/x"))

        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(request())
    assert completed == []
