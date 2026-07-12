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

    def safe_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "code": self.code,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "model": self.model,
            "model_count": self.model_count,
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


def _response_event_accepted(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    event_type = str(value.get("type") or "")
    response = value.get("response") if isinstance(value.get("response"), dict) else value
    if response.get("error"):
        return False
    if event_type == "response.completed":
        return str(response.get("status") or "") == "completed"
    if response.get("object") == "response" and str(response.get("status") or "") == "completed":
        return True
    if event_type == "response.incomplete" or str(response.get("status") or "") == "incomplete":
        details = response.get("incomplete_details")
        return isinstance(details, dict) and details.get("reason") == "max_output_tokens"
    return False


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


def _valid_responses_stream(response: requests.Response) -> bool:
    consumed = 0
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            return False
        try:
            return _response_event_accepted(json.loads(body))
        except (TypeError, ValueError):
            return False
    accepted = False
    for raw_line in response.iter_lines(decode_unicode=False):
        consumed += len(raw_line)
        if consumed > MAX_RESPONSE_BYTES:
            return False
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line in ("event: error", "event: response.error", "event: response.failed"):
            return False
        if line in ("event: response.completed", "event: response.incomplete"):
            # The following data line still determines whether an incomplete
            # result is the expected one-token cap.
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if _response_event_failed(event):
            return False
        if _response_event_accepted(event):
            accepted = True
    return accepted


def probe_codex_responses(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 30.0,
    allow_regex: str = DEFAULT_ALLOW_RE,
    deny_regex: str = DEFAULT_DENY_RE,
    preferred_models: Iterable[str] = DEFAULT_PREFERRED_MODELS,
    session: requests.Session | None = None,
) -> ProbeResult:
    client = session or requests.Session()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    models_url = _endpoint(base_url, "models")
    try:
        models_response = client.get(
            models_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException:
        return ProbeResult(False, "probe_models_unreachable", True)
    if models_response.is_redirect or not _same_origin(models_url, models_response.url):
        return ProbeResult(False, "probe_cross_origin_redirect", True, models_response.status_code)
    if models_response.status_code != 200:
        return _status_result(models_response.status_code, model=None, model_count=0)
    if len(models_response.content) > MAX_MODELS_BYTES:
        return ProbeResult(False, "probe_models_too_large", True, http_status=200)
    try:
        models = parse_models_payload(models_response.json())
    except ValueError:
        return ProbeResult(False, "probe_models_invalid_json", True, http_status=200)
    model, model_count = select_codex_model(
        models,
        allow_regex=allow_regex,
        deny_regex=deny_regex,
        preferred_models=preferred_models,
    )
    if model is None:
        return ProbeResult(False, "probe_no_codex_model", True, http_status=200)

    responses_url = _endpoint(base_url, "responses")
    response_headers = {**headers, "Content-Type": "application/json", "Accept": "text/event-stream"}
    try:
        response = client.post(
            responses_url,
            headers=response_headers,
            json={
                "model": model,
                "input": "hi",
                "max_output_tokens": 1,
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
            model=model,
            model_count=model_count,
        )
    if response.is_redirect or not _same_origin(responses_url, response.url):
        return ProbeResult(
            False,
            "probe_cross_origin_redirect",
            True,
            http_status=response.status_code,
            model=model,
            model_count=model_count,
        )
    if response.status_code != 200:
        return _status_result(
            response.status_code,
            model=model,
            model_count=model_count,
        )
    if not _valid_responses_stream(response):
        return ProbeResult(
            False,
            "probe_invalid_responses_protocol",
            True,
            http_status=200,
            model=model,
            model_count=model_count,
        )
    return ProbeResult(
        True,
        "compatible",
        False,
        http_status=200,
        model=model,
        model_count=model_count,
    )
