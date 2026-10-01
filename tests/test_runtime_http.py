import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypedDict

import pytest

from inflowpay import ClientOptions, InflowApiError
from inflowpay._runtime import Client

# Snapshot of inflow-specs fixtures/runtime.mjs at 3bcc2a099ba5f9d9fecb7154a952500336d113c8.
SCENARIOS = json.loads(Path(__file__).with_name("fixtures").joinpath("runtime.json").read_text())


class Response(TypedDict, total=False):
    status: int
    headers: dict[str, str]
    json: object


class Request(TypedDict):
    method: str
    path: str
    headers: dict[str, str]


class Exchange(TypedDict):
    request: Request
    response: Response


@asynccontextmanager
async def server(exchanges: list[Exchange]) -> AsyncIterator[str]:
    seen: list[tuple[str, str, dict[str, str]]] = []
    tasks: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            first, *lines = head.split("\r\n")
            method, path, _ = first.split(" ")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines if line)
            }
            await reader.readexactly(int(headers.get("content-length", "0")))
            index = len(seen)
            seen.append((method, path, headers))
            response = exchanges[index]["response"]
            body = json.dumps(response["json"]).encode() if "json" in response else b""
            wire = (
                f"HTTP/1.1 {response['status']} Test\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n"
            )
            for name, value in response.get("headers", {}).items():
                wire += f"{name}: {value}\r\n"
            writer.write(wire.encode() + b"\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.create_task(handle(reader, writer)))

    listener = await asyncio.start_server(connect, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    finally:
        listener.close()
        await listener.wait_closed()
        await asyncio.gather(*tasks)
    assert len(seen) == len(exchanges)
    for actual, exchange in zip(seen, exchanges, strict=True):
        expected = exchange["request"]
        assert actual[:2] == (expected["method"], expected["path"])
        for name in ("x-api-key", "authorization"):
            assert actual[2].get(name) == expected["headers"].get(name)


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_shared_runtime_scenario(name: str) -> None:
    exchanges: list[Exchange] = SCENARIOS[name]["exchanges"]
    headers = exchanges[0]["request"]["headers"]

    async def token() -> str:
        return headers["authorization"].removeprefix("Bearer ")

    async with (
        server(exchanges) as base,
        Client(
            ClientOptions(
                base_url=base,
                api_key=headers.get("x-api-key"),
                access_token=token if "authorization" in headers else None,
            )
        ) as client,
    ):
        for exchange in exchanges:
            request, response = exchange["request"], exchange["response"]
            if response["status"] >= 400:
                with pytest.raises(InflowApiError) as failure:
                    await client.request(request["method"], request["path"])
                assert failure.value.http_status == response["status"]
                assert failure.value.body == response.get("json")
                body = response.get("json")
                if isinstance(body, dict):
                    assert failure.value.code == body["errors"][0]["code"]
                    assert str(failure.value) == body["errors"][0]["message"]
                else:
                    assert failure.value.code == "UNEXPECTED_ERROR"
            else:
                assert await client.request(request["method"], request["path"]) == response.get(
                    "json"
                )


async def test_real_http_redirect_is_not_followed() -> None:
    exchanges: list[Exchange] = [
        {
            "request": {
                "method": "GET",
                "path": "/start",
                "headers": {"x-api-key": "test-only-key"},
            },
            "response": {"status": 307, "headers": {"location": "/must-not-follow"}},
        }
    ]
    async with (
        server(exchanges) as base,
        Client(ClientOptions(base_url=base, api_key="test-only-key")) as client,
    ):
        with pytest.raises(InflowApiError) as failure:
            await client.request("GET", "/start", retries=3)
    assert failure.value.http_status == 307
