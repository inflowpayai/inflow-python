import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from inflowpay import ClientOptions, InflowApiError
from inflowpay._runtime import Client

Handler = Callable[[str, dict[str, str], bytes, asyncio.StreamWriter], Awaitable[None]]
OK = b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}'


@asynccontextmanager
async def native_server(handler: Handler) -> AsyncIterator[str]:
    tasks: list[asyncio.Task[None]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode()
            first, *lines = raw.split("\r\n")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines if line)
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            await handler(first.split(" ")[1], headers, body, writer)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.append(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(connect, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()
        assert all(
            result is None or isinstance(result, asyncio.CancelledError) for result in results
        )


@pytest.mark.parametrize(
    "recover,retries,expected_calls", [(False, 0, 1), (False, 1, 2), (True, 1, 2)]
)
async def test_invalid_compression(recover: bool, retries: int, expected_calls: int) -> None:
    calls = 0

    async def handle(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal calls
        calls += 1
        writer.write(
            OK
            if recover and calls > 1
            else (
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 9\r\n"
                b"Connection: close\r\n\r\nnot-gzip!"
            )
        )

    async with native_server(handle) as base, Client(ClientOptions(base_url=base)) as client:
        if recover:
            assert await client.request("GET", "/gzip", retries=retries) == {"ok": True}
        else:
            with pytest.raises(InflowApiError) as failure:
                await client.request("POST", "/gzip", retries=retries)
            assert failure.value.code == "NETWORK_ERROR"
            assert failure.value.http_status == 0
    assert calls == expected_calls


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("recover", [False, True])
async def test_native_timeout_retry(phase: str, recover: bool) -> None:
    calls = 0

    async def handle(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal calls
        calls += 1
        if recover and calls == 2:
            writer.write(OK)
            return
        if phase == "body":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{")
            await writer.drain()
        await asyncio.Event().wait()

    async with (
        native_server(handle) as base,
        Client(ClientOptions(base_url=base, timeout=0.15)) as client,
    ):
        if recover:
            assert await client.request("GET", "/slow", retries=1) == {"ok": True}
        else:
            with pytest.raises(InflowApiError) as failure:
                await client.request("GET", "/slow", retries=1)
            assert failure.value.code == "TIMEOUT"
    assert calls == 2


async def test_parallel_outcomes_are_independent() -> None:
    started = asyncio.Event()
    paths: list[str] = []

    async def handle(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        paths.append(path)
        if path == "/ok":
            writer.write(OK)
            return
        if path == "/cancel":
            started.set()
        await asyncio.Event().wait()

    async with (
        native_server(handle) as base,
        Client(ClientOptions(base_url=base, timeout=0.3)) as client,
    ):
        cancelled = asyncio.create_task(client.request("GET", "/cancel", retries=3))
        success = asyncio.create_task(client.request("GET", "/ok"))
        timeout = asyncio.create_task(client.request("GET", "/slow"))
        await started.wait()
        cancelled.cancel()
        results = await asyncio.gather(success, timeout, cancelled, return_exceptions=True)
        assert results[0] == {"ok": True}
        assert isinstance(results[1], InflowApiError) and results[1].code == "TIMEOUT"
        assert isinstance(results[2], asyncio.CancelledError)
        assert await client.request("GET", "/ok") == {"ok": True}
    assert sorted(paths) == ["/cancel", "/ok", "/ok", "/slow"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("auth", ["key", "bearer", "anonymous"])
async def test_cross_origin_post_redirect(status: int, auth: str) -> None:
    destinations: list[str] = []
    received: list[tuple[dict[str, str], bytes]] = []

    async def target(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        destinations.append(path)
        writer.write(OK)

    async def token() -> str:
        return "test-only-token"

    async with native_server(target) as destination:

        async def source(
            path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
        ) -> None:
            received.append((headers, body))
            writer.write(
                (
                    f"HTTP/1.1 {status} Redirect\r\nLocation: {destination}/forwarded\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode()
            )

        async with (
            native_server(source) as base,
            Client(
                ClientOptions(
                    base_url=base,
                    api_key="test-only-key" if auth == "key" else None,
                    access_token=token if auth == "bearer" else None,
                )
            ) as client,
        ):
            with pytest.raises(InflowApiError) as failure:
                await client.request(
                    "POST", "/pay", body={"signature": "test-only-signature"}, retries=3
                )
            assert failure.value.http_status == status
    assert destinations == [] and len(received) == 1
    headers, body = received[0]
    assert headers.get("x-api-key") == ("test-only-key" if auth == "key" else None)
    assert headers.get("authorization") == ("Bearer test-only-token" if auth == "bearer" else None)
    assert body == b'{"signature": "test-only-signature"}'
