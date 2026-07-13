from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


DEFAULT_ALLOW_RE = r"^(?:gpt-|codex-)"
DEFAULT_DENY_RE = r"(?:^|[-_/])(?:audio|embedding|image|realtime|speech|transcribe|tts|video)(?:$|[-_/])"
DEFAULT_PREFERRED_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex-spark",
)
REQUIRED_MODEL = "gpt-5.6-sol"
PROBE_POLICY = "strict-gpt-5.6-sol-responses-v2"
RESPONSE_CONTRACT = "openai-responses-v1-terminal-v2"
MAX_MODELS_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 512_000


@dataclass(frozen=True)
class ProbeResult:
    compatible: bool
    code: str
    retryable: bool
    http_status: int | None = None
    model: str | None = None
    model_count: int = 0
    response_id: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "code": self.code,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "model": self.model,
            "model_count": self.model_count,
            "response_id": self.response_id,
        }


def _endpoint(base_url: str, path: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    suffix = path.strip("/")
    if cleaned.endswith(f"/{suffix}"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/{suffix}"
    return f"{cleaned}/v1/{suffix}"


def _same_origin(left: str, right: str) -> bool:
    a = urlparse(left)
    b = urlparse(right)
    return (a.scheme, a.hostname, a.port or (443 if a.scheme == "https" else 80)) == (
        b.scheme,
        b.hostname,
        b.port or (443 if b.scheme == "https" else 80),
    )


def parse_models_payload(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            item = value.strip()
            if item:
                found.append(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        nested = [
            key
            for key in ("data", "models", "items")
            if isinstance(value.get(key), (list, dict))
        ]
        if nested:
            for key in nested:
                walk(value[key])
            return
        for key in ("id", "model", "name"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                found.append(raw.strip())
                return

    walk(payload)
    seen: set[str] = set()
    result: list[str] = []
    for item in found:
        if len(item) <= 160 and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def select_codex_model(
    models: Iterable[str],
    *,
    allow_regex: str = DEFAULT_ALLOW_RE,
    deny_regex: str = DEFAULT_DENY_RE,
    preferred_models: Iterable[str] = DEFAULT_PREFERRED_MODELS,
) -> tuple[str | None, int]:
    allow = re.compile(allow_regex, re.I)
    deny = re.compile(deny_regex, re.I)
    candidates = sorted(
        {
            str(model).strip()
            for model in models
            if str(model).strip()
            and allow.search(str(model).strip())
            and not deny.search(str(model).strip())
        }
    )
    for preferred in preferred_models:
        if preferred in candidates:
            return preferred, len(candidates)
    if not candidates:
        return None, 0

    def rank(model: str) -> tuple[Any, ...]:
        numbers = [int(value) for value in re.findall(r"\d+", model)][:5]
        numbers += [0] * (5 - len(numbers))
        lowered = model.lower()
        return (
            *numbers,
            1 if re.search(r"(?:^|[-_/])sol(?:$|[-_/])", lowered) else 0,
            1 if "codex" in lowered else 0,
            -len(lowered),
            lowered,
        )

    return max(candidates, key=rank), len(candidates)


def _status_result(status: int, *, model: str | None, model_count: int) -> ProbeResult:
    if status in (429, 500, 502, 503, 504):
        return ProbeResult(
            False,
            "probe_temporarily_unavailable",
            True,
            http_status=status,
            model=model,
            model_count=model_count,
        )
    if status in (401, 402, 403):
        return ProbeResult(
            False,
            "probe_key_rejected",
            True,
            http_status=status,
            model=model,
            model_count=model_count,
        )
    return ProbeResult(
        False,
        "probe_responses_rejected",
        True,
        http_status=status,
        model=model,
        model_count=model_count,
    )


def _terminal_response(
    value: Any,
    *,
    required_model: str,
) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, None
    event_type = str(value.get("type") or "")
    response = value.get("response") if isinstance(value.get("response"), dict) else value
    if response.get("error"):
        return False, None
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id.strip():
        return False, None
    if response.get("object") != "response":
        return False, None
    if response.get("model") != required_model:
        return False, None
    output = response.get("output")
    usage = response.get("usage")
    if not isinstance(output, list) or not output:
        return False, None
    if not isinstance(usage, dict):
        return False, None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 1
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 1
    ):
        return False, None
    status = str(response.get("status") or "")
    if event_type:
        if event_type not in ("response.completed", "response.incomplete"):
            return False, None
        expected_status = event_type.removeprefix("response.")
        if status != expected_status:
            return False, None
    if status == "completed":
        return True, response_id
    if status == "incomplete":
        details = response.get("incomplete_details")
        return (
            isinstance(details, dict)
            and details.get("reason") == "max_output_tokens",
            response_id,
        )
    return False, None


def _response_event_failed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    event_type = str(value.get("type") or "")
    response = value.get("response") if isinstance(value.get("response"), dict) else value
    status = str(response.get("status") or "")
    return bool(
        event_type in ("error", "response.error", "response.failed")
        or status in ("cancelled", "failed")
        or response.get("error")
    )


def _valid_responses_stream(
    response: requests.Response,
    *,
    required_model: str,
) -> tuple[bool, str | None]:
    consumed = 0
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            return False, None
        try:
            return _terminal_response(json.loads(body), required_model=required_model)
        except (TypeError, ValueError):
            return False, None
    terminal_response_id: str | None = None
    for raw_line in response.iter_lines(decode_unicode=False):
        consumed += len(raw_line)
        if consumed > MAX_RESPONSE_BYTES:
            return False, None
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line in ("event: error", "event: response.error", "event: response.failed"):
            return False, None
        if line in ("event: response.completed", "event: response.incomplete"):
            # The following data line still determines whether an incomplete
            # result is the bounded-output cap used by this probe.
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            return False, None
        if _response_event_failed(event):
            return False, None
        accepted, response_id = _terminal_response(
            event,
            required_model=required_model,
        )
        if accepted:
            if terminal_response_id is not None:
                return False, None
            terminal_response_id = response_id
    return terminal_response_id is not None, terminal_response_id


def probe_codex_responses(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 30.0,
    required_model: str = REQUIRED_MODEL,
    session: requests.Session | None = None,
) -> ProbeResult:
    if required_model != REQUIRED_MODEL:
        return ProbeResult(False, "probe_policy_mismatch", False, model=required_model)
    client = session or requests.Session()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    model_count = 0
    models_url = _endpoint(base_url, "models")
    try:
        models_response = client.get(
            models_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException:
        models_response = None
    # A valid catalog is a cheap admission prefilter: if it proves there are no
    # GPT/Codex text models, do not spend quota on a Responses probe. Relays that
    # omit or break /models still fall through to the exact-model request.
    if (
        models_response is not None
        and not models_response.is_redirect
        and _same_origin(models_url, models_response.url)
        and models_response.status_code == 200
        and len(models_response.content) <= MAX_MODELS_BYTES
    ):
        try:
            models = parse_models_payload(models_response.json())
            _, model_count = select_codex_model(models)
        except (TypeError, ValueError):
            model_count = 0
        else:
            if model_count == 0:
                return ProbeResult(
                    False,
                    "probe_no_codex_model",
                    False,
                    http_status=200,
                    model=required_model,
                    model_count=0,
                )

    responses_url = _endpoint(base_url, "responses")
    response_headers = {**headers, "Content-Type": "application/json", "Accept": "text/event-stream"}
    try:
        response = client.post(
            responses_url,
            headers=response_headers,
            json={
                "model": required_model,
                "input": "Reply with OK.",
                "max_output_tokens": 8,
                "stream": True,
                "store": False,
            },
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException:
        return ProbeResult(
            False,
            "probe_responses_unreachable",
            True,
            model=required_model,
            model_count=model_count,
        )
    if response.is_redirect or not _same_origin(responses_url, response.url):
        return ProbeResult(
            False,
            "probe_cross_origin_redirect",
            True,
            http_status=response.status_code,
            model=required_model,
            model_count=model_count,
        )
    if response.status_code != 200:
        return _status_result(
            response.status_code,
            model=required_model,
            model_count=model_count,
        )
    try:
        valid_contract, response_id = _valid_responses_stream(
            response,
            required_model=required_model,
        )
    except requests.RequestException:
        return ProbeResult(
            False,
            "probe_responses_unreachable",
            True,
            http_status=200,
            model=required_model,
            model_count=model_count,
        )
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not valid_contract:
        return ProbeResult(
            False,
            "probe_invalid_responses_protocol",
            True,
            http_status=200,
            model=required_model,
            model_count=model_count,
        )
    return ProbeResult(
        True,
        "compatible",
        False,
        http_status=200,
        model=required_model,
        model_count=model_count,
        response_id=response_id,
    )
