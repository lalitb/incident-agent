import json
import os
import re
from pathlib import Path
from time import perf_counter, sleep

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from .gateway import redact


KEY_VARIABLES = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
}
REQUEST_INTERVAL_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 180
_next_request_at = 0


def load_environment():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


class ModelResponseError(ValueError):
    def __init__(self, detail, metadata):
        self.detail = detail
        self.metadata = metadata
        super().__init__(detail)


class ModelCallError(RuntimeError):
    def __init__(self, metadata):
        self.metadata = metadata
        reason = metadata.get("error_type", "unknown provider error")
        if metadata.get("http_status"):
            reason += f" (HTTP {metadata['http_status']})"
        if metadata.get("http_status") == 402:
            reason += ": provider requires payment for this request"
        if metadata.get("retry_stop_reason"):
            reason += f" ({metadata['retry_stop_reason']})"
        super().__init__(redact(reason)[0])


def completion(**kwargs):
    # Collection and offline evaluation do not initialize a provider SDK.
    import litellm

    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    return litellm.completion(**kwargs)


def rate_limit_details(exc):
    # Inspect only to classify the limit; never persist the provider's payload.
    message = str(exc)
    daily = re.search(r'"quotaId"\s*:\s*"[^"]*PerDay[^"]*"|free-models-per-day', message, re.I)
    zero_limit = re.search(r"\blimit:\s*0\b", message, re.I)
    delay = re.search(r'"retryDelay"\s*:\s*"([0-9.]+)s"', message)
    if delay is None:
        delay = re.search(r"retry in ([0-9.]+)s", message, re.I)
    headers = getattr(getattr(exc, "response", None), "headers", {})
    try:
        seconds = max(float(headers.get("retry-after", 0)), float(delay[1]) if delay else 0)
    except (ValueError, TypeError):
        seconds = 0
    return {"kind": "daily_quota" if daily else "zero_quota" if zero_limit else "rate_limit",
            "retry_after_seconds": seconds}


def request_completion(metadata, on_progress=None, **kwargs):
    global _next_request_at

    def checkpoint():
        if on_progress:
            on_progress(redact(metadata)[0])

    metadata["attempts"] = []
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        wait_seconds = max(0, _next_request_at - perf_counter())
        if wait_seconds:
            print(f"Waiting {wait_seconds:.0f}s before model request...", flush=True)
            metadata.update(status="waiting", wait_seconds=round(wait_seconds, 2))
            checkpoint()
            sleep(wait_seconds)
        started = perf_counter()
        _next_request_at = started + REQUEST_INTERVAL_SECONDS
        entry = {"attempt": attempt + 1, "status": "requested"}
        metadata["attempts"].append(entry)
        metadata.update(status="requested", wait_seconds=0)
        checkpoint()
        try:
            response = completion(**kwargs)
        except Exception as exc:
            entry.update(status="failed", error_type=type(exc).__name__)
            if getattr(exc, "status_code", None) != 429:
                checkpoint()
                raise
            details = rate_limit_details(exc)
            entry["rate_limit"] = details
            delay = max(60 * (attempt + 1), details["retry_after_seconds"] + 1)
            stop_reason = None
            if details["kind"] != "rate_limit":
                stop_reason = details["kind"]
            elif attempt == MAX_RATE_LIMIT_RETRIES:
                stop_reason = "retry_budget"
            elif delay > MAX_RETRY_WAIT_SECONDS:
                stop_reason = "retry_wait_budget"
            if stop_reason:
                metadata["retry_stop_reason"] = stop_reason
                checkpoint()
                raise
            entry["retry_wait_seconds"] = delay
            _next_request_at = perf_counter() + delay
            print(f"Rate limited; retry {attempt + 1}/{MAX_RATE_LIMIT_RETRIES} after {delay:.0f}s.", flush=True)
            checkpoint()
            continue
        entry["status"] = "completed"
        return response


def generate_structured(instructions: str, content: str, schema: type[BaseModel], on_progress=None, max_tokens=4096):
    load_environment()
    model = os.getenv("LLM_MODEL", "").strip()
    provider, separator, model_name = model.partition("/")
    if provider == "openrouter" and not model_name.endswith(":free"):
        raise ValueError("Use an explicit OpenRouter model ending in :free")
    metadata = {
        "provider": provider if separator else None,
        "requested_model": model or None,
        "model": None,
        "response_id": None,
        "elapsed_ms": None,
        "usage": None,
        "status": "requested",
        "max_output_tokens": max_tokens,
    }
    started = perf_counter()
    try:
        if provider not in KEY_VARIABLES or not model_name:
            raise RuntimeError("Invalid model configuration")
        api_key = os.getenv(KEY_VARIABLES[provider])
        if not api_key:
            raise RuntimeError("Missing provider credential")

        # A caller may have built its payload before dotenv was loaded.
        content = json.dumps(redact(json.loads(content))[0], allow_nan=False)
        instructions, _ = redact(instructions)
        options = {}
        if provider == "openrouter":
            options["extra_body"] = {"provider": {
                "require_parameters": True,
                "allow_fallbacks": False,
                "max_price": {"prompt": 0, "completion": 0, "request": 0},
            }}
        elif provider == "moonshot" and model_name == "kimi-k2.6":
            # Keep the output budget available for the report, rather than reasoning tokens.
            options["extra_body"] = {"thinking": {"type": "disabled"}}
        elif provider == "moonshot" and model_name == "kimi-k3":
            options["extra_body"] = {"reasoning_effort": "low"}
        response = request_completion(
            metadata,
            on_progress=on_progress,
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": content},
            ],
            response_format=schema,
            max_tokens=max_tokens,
            timeout=120 if provider == "moonshot" else 60,
            num_retries=0,
            **options,
        )
    except Exception as exc:
        metadata.update(status="failed", error_type=type(exc).__name__,
                        http_status=getattr(exc, "status_code", None),
                        elapsed_ms=round((perf_counter() - started) * 1000, 2))
        raise ModelCallError(redact(metadata)[0]) from None

    usage = getattr(response, "usage", None)
    metadata.update(
        status="received",
        model=getattr(response, "model", None),
        response_id=getattr(response, "id", None),
        elapsed_ms=round((perf_counter() - started) * 1000, 2),
        usage=({
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        } if usage is not None else None),
    )
    metadata, _ = redact(metadata)

    def reject(detail):
        metadata["status"] = "invalid_response"
        raise ModelResponseError(detail, metadata)

    if not response.choices:
        reject("Provider returned no response choices")
    choice = response.choices[0]
    metadata["finish_reason"] = (
        choice.finish_reason if choice.finish_reason in
        {"stop", "length", "content_filter", "tool_calls", "function_call"} else "unknown"
    )
    if choice.finish_reason == "length":
        reject(f"Model response reached its output-token limit ({max_tokens})")
    if choice.finish_reason != "stop":
        reject("Model response did not finish normally")
    if getattr(choice.message, "refusal", None):
        reject("The model refused the request")
    if not isinstance(choice.message.content, str) or not choice.message.content.strip():
        reject("The model returned no response text")

    try:
        result = schema.model_validate_json(choice.message.content)
    except ValidationError as exc:
        # Never include Pydantic's input values or provider response text.
        error_types = sorted({error["type"] for error in exc.errors()})
        reject("Response schema validation failed: " + ", ".join(error_types))

    metadata["status"] = "completed"
    return result, metadata
