import inspect
import json
import os
import re
from time import perf_counter
from types import MappingProxyType

from .tools.common import (
    ToolError,
    validate_service,
    validate_window,
)
from .tools.telemetry import (
    find_traces,
    get_trace,
    query_metrics,
    search_logs,
)


# Explicit registry. Never resolve a model-supplied name using eval()
# or arbitrary imports.
TOOLS = MappingProxyType({
    "query_metrics": query_metrics,
    "find_traces": find_traces,
    "get_trace": get_trace,
    "search_logs": search_logs,
})

ARGUMENT_TYPES = {
    "service": str,
    "start": str,
    "end": str,
    "metric": str,
    "trace_id": str,
    "contains": str,
    "limit": int,
    "min_duration_ms": int,
}

MAX_ARGUMENT_BYTES = 4096
MAX_RESULT_BYTES = 100_000

SENSITIVE_KEYS = {
    "authorization",
    "proxyauthorization",
    "password",
    "passwd",
    "secret",
    "clientsecret",
    "token",
    "accesstoken",
    "refreshtoken",
    "apikey",
    "cookie",
    "setcookie",
    "email",
    "connectionstring",
    "databaseurl",
}

# These cover specific common patterns, not every possible secret.
TEXT_RULES = [
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(
            r"\b(?:password|passwd|api[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|client[_-]?secret|secret|token|cookie)"
            r"[\"']?\s*[:=]\s*"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
            re.I,
        ),
        "[REDACTED_CREDENTIAL]",
    ),
    (
        re.compile(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            re.I,
        ),
        "[REDACTED_EMAIL]",
    ),
    (
        re.compile(
            r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@",
            re.I,
        ),
        "[REDACTED_CONNECTION_CREDENTIALS]@",
    ),
    (
        re.compile(r"/Users/[^/\s]+"),
        "/Users/[REDACTED_USER]",
    ),
    (re.compile(r"\bBasic\s+[A-Za-z0-9+/=]+", re.I), "Basic [REDACTED]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"), "[REDACTED_API_KEY]"),
]


class RejectedCall(ValueError):
    pass


def redact(value):
    """Return a sanitized copy and the number of replacements."""
    replacements = 0
    credentials = [
        secret for name, secret in os.environ.items()
        if secret and any(part in name.upper() for part in
                          ("API_KEY", "TOKEN", "PASSWORD", "SECRET"))
    ]

    def walk(item):
        nonlocal replacements

        if isinstance(item, dict):
            result = {}

            for key, child in item.items():
                normalized = re.sub(
                    r"[^a-z0-9]", "", str(key).lower()
                )

                safe_key = walk(key) if isinstance(key, str) else key
                if normalized in SENSITIVE_KEYS or any(
                    normalized.endswith(suffix)
                    for suffix in ("apikey", "password", "accesstoken", "secret")
                ):
                    result[safe_key] = "[REDACTED]"
                    replacements += 1
                else:
                    result[safe_key] = walk(child)

            return result

        if isinstance(item, list):
            return [walk(child) for child in item]

        if isinstance(item, str):
            for secret in credentials:
                if secret in item:
                    replacements += item.count(secret)
                    item = item.replace(secret, "[REDACTED]")
            for pattern, replacement in TEXT_RULES:
                item, count = pattern.subn(replacement, item)
                replacements += count
            return item

        return item

    return walk(value), replacements


class ToolGateway:
    def __init__(self, service, start, end):
        # These values come from application/controller configuration,
        # not from retrieved logs or model tool arguments.
        validate_service(service)
        self._start, self._end = validate_window(start, end)
        self._service = service

    def _validate(self, tool_name, arguments):
        if not isinstance(tool_name, str) or tool_name not in TOOLS:
            raise RejectedCall("Tool is not allowed")

        if not isinstance(arguments, dict):
            raise RejectedCall("arguments must be a JSON object")

        try:
            encoded = json.dumps(
                arguments,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise RejectedCall("Arguments must contain valid JSON values")

        if len(encoded) > MAX_ARGUMENT_BYTES:
            raise RejectedCall("Arguments exceed the size limit")

        function = TOOLS[tool_name]

        try:
            bound = inspect.signature(function).bind(**arguments)
        except TypeError:
            raise RejectedCall(
                "Missing required arguments or unexpected argument names"
            )

        bound.apply_defaults()

        for name, value in bound.arguments.items():
            expected = ARGUMENT_TYPES[name]

            # Exact type check rejects True as an integer.
            if type(value) is not expected:
                raise RejectedCall(
                    f"{name} must have type {expected.__name__}"
                )

        values = bound.arguments

        if values["service"] != self._service:
            raise RejectedCall(
                "Service is outside this investigation"
            )

        try:
            start, end = validate_window(
                values["start"], values["end"]
            )
        except ValueError:
            raise RejectedCall(
                "Invalid time window; use timezone-aware ISO timestamps "
                "and a positive interval of at most one hour"
            )

        if start < self._start or end > self._end:
            raise RejectedCall(
                "Time window is outside this investigation"
            )

        # Tool-specific checks, such as metric choices and result limits,
        # remain in the existing tool functions.
        return function, dict(values)

    def execute(self, tool_name, arguments):
        started = perf_counter()

        def failure(code, message):
            # Do not echo raw arguments or backend exception messages.
            return {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                },
                "elapsed_ms": round(
                    (perf_counter() - started) * 1000, 2
                ),
            }

        try:
            function, validated = self._validate(
                tool_name, arguments
            )
        except RejectedCall as exc:
            return failure("rejected", str(exc))

        try:
            raw_result = function(**validated)
        except ToolError:
            return failure(
                "backend_error",
                "The backend query failed. Check backend health, "
                "the time window and whether the trace exists.",
            )
        except ValueError:
            return failure(
                "invalid_arguments_or_data",
                "A tool-specific value or backend data format was invalid.",
            )
        except Exception:
            return failure(
                "internal_error",
                "The tool could not complete. Inspect it locally.",
            )

        # This is a basic envelope check. Detailed backend validation
        # belongs in the individual adapters.
        if (
            not isinstance(raw_result, dict)
            or raw_result.get("tool") != tool_name
            or not isinstance(raw_result.get("evidence_id"), str)
            or not isinstance(raw_result.get("data"), list)
        ):
            return failure(
                "invalid_result",
                "Tool returned an invalid evidence envelope",
            )

        try:
            sanitized, count = redact(raw_result)

            # Applies to every backend, not only logs.
            sanitized["content_is_untrusted"] = True

            encoded = json.dumps(
                sanitized,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return failure(
                "invalid_result",
                "Tool result could not be safely serialized",
            )

        if len(encoded) > MAX_RESULT_BYTES:
            return failure(
                "result_too_large",
                "Narrow the time window or reduce the result limit.",
            )

        return {
            "ok": True,
            "result": sanitized,
            "guardrails": {
                "redaction_replacements": count,
                "result_bytes": len(encoded),
            },
            "elapsed_ms": round(
                (perf_counter() - started) * 1000, 2
            ),
        }
