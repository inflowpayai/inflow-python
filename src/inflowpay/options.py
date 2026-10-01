from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx


@dataclass(frozen=True, kw_only=True)
class ClientOptions:
    environment: Literal["production", "sandbox"] = "production"
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    access_token: Callable[[], Awaitable[str]] | None = field(default=None, repr=False)
    timeout: float = 30.0
    # Ownership transfers to the SDK client; do not share this transport between clients.
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
