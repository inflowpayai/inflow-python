from collections.abc import Mapping


class InflowApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int,
        endpoint: str,
        body: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.endpoint = endpoint
        self.body = body
        self.headers = dict(headers or {})
        self.request_id = self.headers.get("x-request-id")
