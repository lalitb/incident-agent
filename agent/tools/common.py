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
    pass


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
            f"{backend} returned HTTP {code} for {path}"
        ) from exc

    except (URLError, TimeoutError, OSError) as exc:
        raise ToolError(
            f"Cannot query {backend}: {exc}"
        ) from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise ToolError(
            f"{backend} response exceeded the size limit"
        )

    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolError(
            f"{backend} returned invalid JSON"
        ) from exc

    if not isinstance(result, dict):
        raise ToolError(f"{backend} returned an unexpected response")

    if result.get("status") == "error":
        raise ToolError(
            f"{backend} query failed: {result.get('error', 'unknown error')}"
        )

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