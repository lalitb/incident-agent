import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


BACKENDS = {
    "prometheus": "http://localhost:9090",
    "tempo": "http://localhost:3200",
    "loki": "http://localhost:3100",
}

ALLOWED_SERVICES = {"checkout"}
MAX_WINDOW_SECONDS = 3600
MAX_RESPONSE_BYTES = 2_000_000


class ToolError(RuntimeError):
    def __init__(self, message="The backend query failed.", *, code="backend_error", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def validate_service(service):
    if service not in ALLOWED_SERVICES:
        raise ValueError(
            f"Unsupported service: {service}. "
            f"Allowed: {sorted(ALLOWED_SERVICES)}"
        )


def validate_window(start, end):
    """Accept ISO timestamps with an explicit timezone."""

    def parse(value):
        result = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        if result.tzinfo is None:
            raise ValueError("Timestamps must include a timezone")
        return result.timestamp()

    start_seconds = parse(start)
    end_seconds = parse(end)

    if not 0 < end_seconds - start_seconds <= MAX_WINDOW_SECONDS:
        raise ValueError("Time window must be positive and at most one hour")

    return start_seconds, end_seconds


def validate_limit(limit, maximum):
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}")


def fetch_json(backend, path, params=None):
    url = BACKENDS[backend] + path
    if params:
        url += "?" + urlencode(params)

    request = Request(
        url,
        headers={"Accept": "application/json"},
    )

    try:
        with urlopen(request, timeout=15) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)

    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise ToolError(
            code="backend_unavailable" if code in {408, 429, 500, 502, 503, 504} else "backend_rejected",
            retryable=code in {408, 429, 500, 502, 503, 504},
        ) from None

    except (URLError, TimeoutError, OSError):
        raise ToolError(code="backend_unavailable", retryable=True) from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise ToolError(code="response_too_large")

    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ToolError(code="invalid_backend_data") from None

    if not isinstance(result, dict):
        raise ToolError(code="invalid_backend_data")

    if result.get("status") == "error":
        # Classify the documented Prometheus error type, never the response text.
        retryable = result.get("errorType") in {"timeout", "canceled", "unavailable"}
        raise ToolError(code="backend_error", retryable=retryable)

    return result


def evidence(tool, backend, path, params, data, **metadata):
    return {
        "evidence_id": uuid4().hex,
        "tool": tool,
        "source": BACKENDS[backend] + path,
        "query_parameters": params,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        **metadata,
    }