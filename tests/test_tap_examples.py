import asyncio
import runpy
import socket
from dataclasses import replace
from typing import cast

import httpx
import pytest
import uvicorn
from examples import tap_seller
from fastapi import FastAPI

from inflowpay.tap.seller import TapVerifier
from test_tap_seller import NOW, Resolver, signed


@pytest.mark.parametrize(
    "origin",
    [
        "/relative",
        "ftp://host/",
        "https://host/path",
        "https://host/?x=1",
        "https://user@host",
        "https://host/#x",
    ],
)
def test_example_requires_origin(origin: str) -> None:
    verifier = TapVerifier(key_resolver=Resolver())
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN"):
        tap_seller.create_app(verifier, origin)


@pytest.mark.parametrize("body", [None, b"", b"{}"])
async def test_http_example_accepts_real_signed_request_then_rejects_replay(
    body: bytes | None,
) -> None:
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        app = tap_seller.create_app(verifier, "https://public.example")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://private"
        ) as client:
            request = signed(
                method="GET" if body is None else "POST",
                body=body,
                url="https://public.example/api/catalog?x=1&x=2",
            )
            for status in (200, 401):
                response = await client.request(
                    request.method,
                    "/api/catalog?x=1&x=2",
                    content=body,
                    headers={
                        **cast(dict[str, str], request.headers),
                        "Forwarded": "host=evil.example",
                    },
                )
                assert response.status_code == status
            assert response.json() == {"error": "TAP verification failed"}
            assert (await client.get("/api/catalog")).status_code == 401
            assert (
                await client.post("/api/catalog", content=b"x" * (1024 * 1024 + 1))
            ).status_code == 413


@pytest.mark.parametrize("method,body", [("POST", b"{}"), ("GET", b"")])
async def test_example_over_real_loopback_http(method: str, body: bytes) -> None:
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        app = tap_seller.create_app(verifier, "https://public.example")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            config = uvicorn.Config(app, log_config=None, log_level="critical", proxy_headers=False)
            server = uvicorn.Server(config)
            running = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if running.done():
                            await running
                            pytest.fail("Example server stopped before startup")
                        await asyncio.sleep(0.001)
                request = signed(body=body, method=method, url="https://public.example/api/catalog")
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{sock.getsockname()[1]}"
                ) as client:
                    outbound = client.build_request(
                        method,
                        "/api/catalog",
                        content=request.body,
                        headers=cast(dict[str, str], request.headers),
                    )
                    if method == "GET":
                        assert "content-length" not in outbound.headers
                        assert "transfer-encoding" not in outbound.headers
                    tampered = client.build_request(
                        method,
                        "/api/catalog",
                        content=request.body,
                        headers={**cast(dict[str, str], request.headers), "content-digest": "bad"},
                    )
                    assert (await client.send(tampered)).status_code == 401
                    response = await client.send(outbound)
                    assert response.status_code == 200
                    assert response.json()["agent"]["keyid"] == "test-key"
                    assert (await client.send(outbound)).status_code == 401
                    assert (await client.get("/api/catalog")).status_code == 401
            finally:
                server.should_exit = True
                await asyncio.wait_for(running, 5)


async def test_chunked_empty_body_and_query_tamper() -> None:
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        app = tap_seller.create_app(verifier, "https://public.example")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://private"
        ) as client:
            request = signed(body=b"", method="POST", url="https://public.example/api/catalog")
            outbound = client.build_request(
                "POST", "/api/catalog", content=b"", headers=cast(dict[str, str], request.headers)
            )
            del outbound.headers["content-length"]
            outbound.headers["transfer-encoding"] = "chunked"
            assert (await client.send(outbound)).status_code == 200
            request = replace(signed(url="https://public.example/api/catalog?x=1"), method="GET")
            assert (
                await client.get("/api/catalog?x=2", headers=cast(dict[str, str], request.headers))
            ).status_code == 401


def test_entry_requires_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(tap_seller.__file__, run_name="__main__")
    assert error.value.code == 1
    assert "PUBLIC_ORIGIN" in capsys.readouterr().err


@pytest.mark.parametrize("interrupt", [False, True])
def test_entry_outcomes(interrupt: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        if interrupt:
            raise KeyboardInterrupt

    monkeypatch.setattr(tap_seller, "run", run)
    assert tap_seller.main() == (130 if interrupt else 0)


async def test_run_serves_example(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://public.example")

    async def serve(server: uvicorn.Server, sockets: object = None) -> None:
        assert server.config.port == 3002 and not server.config.proxy_headers
        assert isinstance(server.config.app, FastAPI)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(server.config.app), base_url="http://private"
        ) as client:
            assert (await client.get("/api/catalog")).status_code == 401

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    await tap_seller.run()
