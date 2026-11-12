from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import replace

from fastapi import Request, Response
from x402 import x402ResourceServer
from x402.http.middleware.fastapi import FastAPIAdapter
from x402.http.middleware.fastapi import payment_middleware as upstream_payment_middleware
from x402.http.types import (
    HTTPRequestContext,
    PaymentOption,
    PaywallConfig,
    RouteConfig,
    RouteConfigurationError,
)
from x402.http.x402_http_server import PaywallProvider, x402HTTPResourceServer
from x402.schemas.hooks import GrantAccessResult

RouteResolver = Callable[[Request], RouteConfig | Awaitable[RouteConfig]]
Middleware = Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]


def _copy_option(option: PaymentOption) -> PaymentOption:
    return replace(
        option,
        price=option.price if callable(option.price) else deepcopy(option.price),
        extra=deepcopy(option.extra),
    )


def _observe_initialization(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


def payment_middleware(
    routes: Mapping[str, RouteConfig | RouteResolver],
    server: x402ResourceServer,
    paywall_config: PaywallConfig | None = None,
    paywall_provider: PaywallProvider | None = None,
) -> Middleware:
    """Resolve complete routes per request, then delegate to upstream payment middleware.

    Resolvers receive a Starlette request; explicit body reads use its replay cache.
    Keep the server and its facilitator clients open for the application lifetime.
    """
    configured = dict(routes)
    declarations = {pattern: RouteConfig(accepts=[]) for pattern in configured}
    patterns = {id(route): pattern for pattern, route in declarations.items()}
    initialization: asyncio.Task[None] | None = None

    async def middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonlocal initialization
        matched: str | None = None

        def select(_: HTTPRequestContext, route: RouteConfig) -> GrantAccessResult:
            nonlocal matched
            matched = patterns[id(route)]
            return GrantAccessResult()

        # Let upstream select the route, including precedence and encoded paths.
        router = x402HTTPResourceServer(server, declarations)
        router.on_protected_request(select)
        path = request.url.path
        root = request.scope.get("root_path", "")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root) :]
        await router.process_http_request(
            HTTPRequestContext(
                adapter=FastAPIAdapter(request),
                path=request.scope["raw_path"].decode("ascii").split("?")[0],
                decoded_path=path,
                method=request.method,
            )
        )
        if matched is None:
            return await call_next(request)
        pattern = matched
        route = configured[pattern]
        if callable(route):
            value = route(request)
            route = await value if inspect.isawaitable(value) else value
        if not route.accepts:
            raise ValueError("Request pricing requires at least one payment offer")
        route = replace(
            route,
            accepts=(
                _copy_option(route.accepts)
                if isinstance(route.accepts, PaymentOption)
                else [_copy_option(option) for option in route.accepts]
            ),
            extensions=deepcopy(route.extensions),
            tags=deepcopy(route.tags),
        )
        if initialization is None or (
            initialization.done() and initialization.exception() is not None
        ):
            initialization = asyncio.create_task(asyncio.to_thread(server.initialize))
            initialization.add_done_callback(_observe_initialization)
        # Keep initialization independent of a cancelled request, including late failures.
        task = initialization
        await asyncio.wait((task,))
        task.result()
        protected = upstream_payment_middleware(
            {pattern: route},
            server,
            paywall_config,
            paywall_provider,
            sync_facilitator_on_start=False,
        )
        # Upstream initialize() also refreshes facilitators; use its route-only validation here.
        errors = x402HTTPResourceServer(server, {pattern: route})._validate_route_configuration()
        if errors:
            raise RouteConfigurationError(errors)
        return await protected(request, call_next)

    return middleware
