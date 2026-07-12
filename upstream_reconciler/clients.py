from __future__ import annotations

import json
import hashlib
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import requests
import websocket  # type: ignore[import-not-found]

from .core import MANAGED_PREFIX, ReconcileError, UpstreamKey, UpstreamResource, decimal_value
from .probe import ProbeResult, probe_codex_responses
from .store import keychain_get, keychain_set


DEFAULT_TIMEOUT = 15
RETRYABLE_STATUS = {429, 502, 503, 504}
RETRY_DELAYS = (0.5, 1.5)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_api_time(value: Any, *, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ReconcileError("incomplete_scan", f"{field_name} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconcileError("incomplete_scan", f"{field_name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ReconcileError("incomplete_scan", f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _subscription_is_current(
    item: dict[str, Any],
    *,
    field_name: str,
    require_expiry: bool,
    now: datetime | None = None,
) -> bool:
    if item.get("status") != "active":
        return False
    current = now or _utc_now()
    starts_at = _parse_api_time(item.get("starts_at"), field_name=f"{field_name}.starts_at")
    expires_at = _parse_api_time(item.get("expires_at"), field_name=f"{field_name}.expires_at")
    if require_expiry and expires_at is None:
        raise ReconcileError("incomplete_scan", f"{field_name}.expires_at is required")
    if starts_at is not None and current < starts_at:
        return False
    if expires_at is not None and current >= expires_at:
        return False
    return True


def _schema_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {
                str(key): _schema_shape(item, depth=depth + 1)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))[:80]
            },
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "items": [_schema_shape(item, depth=depth + 1) for item in value[:3]],
        }
    return type(value).__name__


def _schema_changed(
    provider_id: str,
    endpoint: str,
    expected: str,
    observed: Any,
) -> ReconcileError:
    shape = _schema_shape(observed)
    raw = json.dumps(shape, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    top_keys = sorted(str(key) for key in observed)[:40] if isinstance(observed, dict) else []
    return ReconcileError(
        "schema_changed",
        f"{provider_id} {endpoint} response schema changed",
        next_action="repeat one read-only doctor check, then follow the upstream maintenance contract",
        context={
            "provider_id": provider_id,
            "endpoint": endpoint,
            "expected": expected,
            "observed_type": type(observed).__name__,
            "observed_keys": top_keys,
            "schema_fingerprint": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        },
    )


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retryable: bool,
    **kwargs: Any,
) -> requests.Response:
    attempts = len(RETRY_DELAYS) + 1 if retryable else 1
    response: requests.Response | None = None
    for attempt in range(attempts):
        response = session.request(method, url, **kwargs)
        if response.status_code not in RETRYABLE_STATUS or attempt == attempts - 1:
            return response
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else RETRY_DELAYS[attempt]
        except ValueError:
            delay = RETRY_DELAYS[attempt]
        time.sleep(max(0.0, min(delay, 5.0)))
    assert response is not None
    return response


def _response_data(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReconcileError(
            "invalid_api_response",
            f"{response.request.method} {urlparse(response.url).path} returned non-JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ReconcileError("invalid_api_response", "API response is not an object")
    if payload.get("success") is False or payload.get("code") not in (None, 0):
        raise ReconcileError(
            "upstream_api_error",
            f"{response.request.method} {urlparse(response.url).path} reported failure",
        )
    return payload.get("data")


def _require_status(response: requests.Response, expected: tuple[int, ...] = (200, 201)) -> None:
    if response.status_code not in expected:
        if response.status_code in (401, 403):
            raise ReconcileError(
                "auth_required",
                f"{response.request.method} {urlparse(response.url).path} requires renewed authorization",
            )
        if response.status_code == 429:
            raise ReconcileError(
                "rate_limited",
                f"{response.request.method} {urlparse(response.url).path} remained rate limited after bounded retries",
                next_action="leave state unchanged and retry on the next scheduled scan",
            )
        if response.status_code in (502, 503, 504):
            raise ReconcileError(
                "upstream_unavailable",
                f"{response.request.method} {urlparse(response.url).path} remained unavailable after bounded retries",
                next_action="leave state unchanged and retry on the next scheduled scan",
            )
        raise ReconcileError(
            "http_error",
            f"{response.request.method} {urlparse(response.url).path} returned HTTP {response.status_code}",
        )


def edge_targets(host: str = "127.0.0.1", port: int = 9222) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/list", timeout=3) as response:
            value = json.load(response)
    except Exception as exc:
        raise ReconcileError(
            "edge_cdp_unavailable",
            f"Edge CDP is not available at {host}:{port}",
            next_action="start Edge with localhost remote debugging and keep the sites logged in",
        ) from exc
    if not isinstance(value, list):
        raise ReconcileError("edge_cdp_invalid", "Edge CDP target list is invalid")
    return [item for item in value if isinstance(item, dict)]


def find_edge_page(origin: str, *, host: str = "127.0.0.1", port: int = 9222) -> dict[str, Any]:
    expected = urlparse(origin)
    for target in edge_targets(host, port):
        if target.get("type") != "page":
            continue
        actual = urlparse(str(target.get("url") or ""))
        if actual.scheme == expected.scheme and actual.netloc == expected.netloc:
            return target
    raise ReconcileError(
        "edge_page_missing",
        f"no logged-in Edge page is open for {expected.netloc}",
        next_action=f"open {origin} in the existing Edge profile and log in",
    )


def cdp_evaluate(target: dict[str, Any], expression: str) -> Any:
    try:
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
        )
        try:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {"expression": expression, "returnByValue": True},
                    }
                )
            )
            while True:
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    break
        finally:
            ws.close()
    except Exception as exc:
        raise ReconcileError("edge_cdp_failed", "could not read the logged-in Edge page") from exc
    return message.get("result", {}).get("result", {}).get("value")


def cdp_cookies(target: dict[str, Any], origin: str) -> list[dict[str, Any]]:
    try:
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
        )
        try:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Network.getCookies",
                        "params": {"urls": [origin.rstrip("/") + "/"]},
                    }
                )
            )
            while True:
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    break
        finally:
            ws.close()
    except Exception as exc:
        raise ReconcileError("edge_cdp_failed", "could not read the logged-in Edge session") from exc
    cookies = message.get("result", {}).get("cookies", [])
    return [item for item in cookies if isinstance(item, dict)]


@dataclass
class ProviderSnapshot:
    provider_id: str
    resources: list[UpstreamResource]
    keys: list[UpstreamKey]
    raw_keys: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def key_by_id(self, key_id: str | int | None) -> UpstreamKey | None:
        if key_id is None:
            return None
        needle = str(key_id)
        return next((item for item in self.keys if item.id == needle), None)

    def keys_by_name(self, name: str) -> list[UpstreamKey]:
        return [item for item in self.keys if item.name == name]


class ProviderClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.provider_id = str(config["id"])
        self.session = requests.Session()

    def scan(self) -> ProviderSnapshot:  # pragma: no cover - interface
        raise NotImplementedError

    def create_key(self, resource: UpstreamResource) -> UpstreamKey:  # pragma: no cover
        raise NotImplementedError

    def create_probe_key(self, resource: UpstreamResource) -> UpstreamKey:  # pragma: no cover
        raise NotImplementedError

    def rename_key(self, key: UpstreamKey, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def enable_key(self, key: UpstreamKey) -> None:  # pragma: no cover
        raise NotImplementedError

    def disable_key(self, key: UpstreamKey) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete_key(self, key_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def reveal_key(self, key: UpstreamKey) -> UpstreamKey:
        return key

    def probe_resource(self, resource: UpstreamResource, key: UpstreamKey) -> ProbeResult:
        preferred = self.config.get("probe_preferred_models")
        return probe_codex_responses(
            str(self.config["inference_base"]),
            key.secret,
            timeout=float(self.config.get("probe_timeout_seconds", 30)),
            allow_regex=str(
                self.config.get("probe_model_allow_regex") or r"^(?:gpt-|codex-)"
            ),
            deny_regex=str(
                self.config.get("probe_model_deny_regex")
                or r"(?:^|[-_/])(?:audio|embedding|image|realtime|speech|transcribe|tts|video)(?:$|[-_/])"
            ),
            preferred_models=(
                [str(value) for value in preferred]
                if isinstance(preferred, list)
                else ("gpt-5.6-sol", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex-spark")
            ),
        )

    def login_with_credentials(self, account: str, password: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def secret_account(self, suffix: str) -> str:
        return f"provider:{self.provider_id}:{suffix}"

    def _stored_login(self) -> tuple[str, str]:
        account = keychain_get(self.secret_account("login_account"), required=False)
        password = keychain_get(self.secret_account("login_password"), required=False)
        if not account or not password:
            raise ReconcileError(
                "auth_required",
                f"{self.provider_id} has no stored API login",
                next_action=f"run enroll-login --provider {self.provider_id}",
                context={"provider_id": self.provider_id},
            )
        return account, password

    def _login_from_keychain(self) -> None:
        account, password = self._stored_login()
        self.login_with_credentials(account, password)


class Sub2APIProvider(ProviderClient):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.api_base = str(config["api_base"]).rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = keychain_get(self.secret_account("access_token"))
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def login_with_credentials(self, account: str, password: str) -> None:
        response = self.session.post(
            f"{self.api_base}/auth/login",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"email": account, "password": password},
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code in (400, 401, 403):
            raise ReconcileError(
                "interactive_auth_required",
                f"{self.provider_id} rejected the stored API login",
                next_action="check whether the password changed or the site added captcha or 2FA",
                context={"provider_id": self.provider_id},
            )
        _require_status(response)
        try:
            data = _response_data(response)
        except ReconcileError as exc:
            if exc.code in ("invalid_api_response", "upstream_api_error"):
                raise ReconcileError(
                    "interactive_auth_required",
                    f"{self.provider_id} rejected the stored API login",
                    next_action="check whether the password changed or the site added captcha or 2FA",
                    context={"provider_id": self.provider_id},
                ) from exc
            raise
        if not isinstance(data, dict) or not data.get("access_token") or not data.get(
            "refresh_token"
        ):
            raise ReconcileError(
                "auth_required",
                f"{self.provider_id} login did not return a reusable session",
                context={"provider_id": self.provider_id},
            )
        keychain_set(self.secret_account("access_token"), str(data["access_token"]))
        keychain_set(self.secret_account("refresh_token"), str(data["refresh_token"]))

    def _refresh(self) -> None:
        refresh_token = keychain_get(self.secret_account("refresh_token"), required=False)
        if not refresh_token:
            self._login_from_keychain()
            return
        response = self.session.post(
            f"{self.api_base}/auth/refresh",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code in (400, 401, 403):
            self._login_from_keychain()
            return
        _require_status(response)
        try:
            data = _response_data(response)
        except ReconcileError as exc:
            if exc.code in ("invalid_api_response", "upstream_api_error"):
                self._login_from_keychain()
                return
            raise
        if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
            self._login_from_keychain()
            return
        keychain_set(self.secret_account("access_token"), str(data["access_token"]))
        keychain_set(self.secret_account("refresh_token"), str(data["refresh_token"]))

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_missing: bool = False,
    ) -> Any:
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        retryable = method in ("GET", "PUT", "DELETE") or idempotency_key is not None
        response = _request_with_retries(
            self.session,
            method,
            f"{self.api_base}{path}",
            retryable=retryable,
            headers=headers,
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code in (401, 403):
            self._refresh()
            headers = self._headers()
            if body is not None:
                headers["Content-Type"] = "application/json"
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            response = _request_with_retries(
                self.session,
                method,
                f"{self.api_base}{path}",
                retryable=retryable,
                headers=headers,
                json=body,
                timeout=DEFAULT_TIMEOUT,
            )
        if response.status_code in (401, 403):
            raise ReconcileError(
                "auth_required",
                f"{self.provider_id} API session could not be renewed",
                next_action="check whether the password changed or the site added captcha or 2FA",
                context={"provider_id": self.provider_id},
            )
        if allow_missing and response.status_code == 404:
            return None
        _require_status(response, (200, 201, 204) if allow_missing else (200, 201))
        if response.status_code == 204 or not response.content:
            return None
        try:
            return _response_data(response)
        except ReconcileError as exc:
            if exc.code != "invalid_api_response":
                raise
            try:
                observed = response.json()
            except ValueError:
                raise
            raise _schema_changed(
                self.provider_id,
                path,
                "standard JSON success envelope",
                observed,
            ) from exc

    def _list_keys(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        page = 1
        pages = 1
        while page <= pages:
            data = self._request("GET", f"/keys?page={page}&page_size=100&sort_by=created_at&sort_order=asc")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise _schema_changed(
                    self.provider_id,
                    "/keys",
                    "object with items array and optional pages",
                    data,
                )
            items.extend(item for item in data["items"] if isinstance(item, dict))
            pages = int(data.get("pages") or 1)
            page += 1
        raw = {str(item.get("id")): item for item in items if item.get("id") is not None}
        return items, raw

    def scan(self) -> ProviderSnapshot:
        available = self._request("GET", "/groups/available")
        subscriptions = self._request("GET", "/subscriptions/active")
        user_rates = self._request("GET", "/groups/rates")
        raw_keys, raw_by_id = self._list_keys()
        if (
            not isinstance(available, list)
            or not isinstance(subscriptions, list)
            or not isinstance(user_rates, dict)
        ):
            observed = {
                "groups_available": available,
                "subscriptions_active": subscriptions,
                "groups_rates": user_rates,
            }
            raise _schema_changed(
                self.provider_id,
                "/groups/available + /subscriptions/active + /groups/rates",
                "array + array + object",
                observed,
            )

        platform = str(self.config.get("include_platform") or "openai")
        adopted_ids = {str(item["key_id"]) for item in self.config.get("adopt", [])}
        groups: dict[str, dict[str, Any]] = {}
        for group in available:
            if not isinstance(group, dict) or group.get("id") is None:
                continue
            if group.get("status") == "active" and group.get("platform") == platform:
                groups[str(group["id"])] = group
        for raw_key in raw_keys:
            if str(raw_key.get("id")) not in adopted_ids and not str(raw_key.get("name") or "").startswith(MANAGED_PREFIX + ":"):
                continue
            group = raw_key.get("group")
            if (
                isinstance(group, dict)
                and group.get("id") is not None
                and group.get("status") == "active"
                and group.get("platform") == platform
            ):
                groups[str(group["id"])] = group

        require_subscription_expiry = bool(
            self.config.get("require_subscription_expiry", False)
        )
        subscription_allowlist = self.config.get("subscription_resource_allowlist")
        allowed_subscription_resources = (
            {str(value) for value in subscription_allowlist}
            if isinstance(subscription_allowlist, list)
            else None
        )
        active_subscription_groups = {
            str(item.get("group_id"))
            for item in subscriptions
            if isinstance(item, dict)
            and item.get("group_id") is not None
            and (
                allowed_subscription_resources is None
                or f"group:{item.get('group_id')}" in allowed_subscription_resources
            )
            and _subscription_is_current(
                item,
                field_name=f"{self.provider_id}/subscription:{item.get('group_id')}",
                require_expiry=require_subscription_expiry,
            )
        }
        excluded_group_ids = {
            str(value) for value in self.config.get("exclude_group_ids", [])
        }
        resources: list[UpstreamResource] = []
        for group_ref, group in groups.items():
            if group_ref in excluded_group_ids:
                continue
            subscription_group = group.get("subscription_type") == "subscription"
            if self.config.get("subscription_only", False) and not subscription_group:
                continue
            resource_id = f"group:{group_ref}"
            if (
                allowed_subscription_resources is not None
                and resource_id in allowed_subscription_resources
                and not subscription_group
            ):
                # An authorized subscription must not silently turn into a
                # metered route when upstream metadata drifts.
                continue
            if (
                subscription_group
                and allowed_subscription_resources is not None
                and resource_id not in allowed_subscription_resources
            ):
                continue
            if subscription_group and group_ref not in active_subscription_groups:
                continue
            source_class = "subscription" if subscription_group else "metered"
            effective_rate = user_rates.get(group_ref, group.get("rate_multiplier"))
            multiplier = None if source_class == "subscription" else decimal_value(
                effective_rate, field_name=f"{self.provider_id}/{group_ref} multiplier"
            )
            resources.append(
                UpstreamResource(
                    provider_id=self.provider_id,
                    resource_id=resource_id,
                    group_ref=group_ref,
                    group_name=str(group.get("name") or group_ref),
                    source_class=source_class,
                    multiplier=multiplier,
                )
            )

        keys = [
            UpstreamKey(
                id=str(item.get("id")),
                name=str(item.get("name") or ""),
                group_ref=str(item.get("group_id") or ""),
                status=str(item.get("status") or ""),
                secret=str(item.get("key") or ""),
            )
            for item in raw_keys
            if item.get("id") is not None
        ]
        return ProviderSnapshot(self.provider_id, resources, keys, raw_by_id)

    def _create_named_key(
        self,
        resource: UpstreamResource,
        *,
        name: str,
        idempotency_key: str,
    ) -> UpstreamKey:
        data = self._request(
            "POST",
            "/keys",
            body={"name": name, "group_id": int(resource.group_ref)},
            idempotency_key=idempotency_key,
        )
        if not isinstance(data, dict) or data.get("id") is None or not data.get("key"):
            raise ReconcileError("create_key_failed", f"{self.provider_id} did not return the created key")
        return UpstreamKey(
            id=str(data["id"]),
            name=str(data.get("name") or name),
            group_ref=str(data.get("group_id") or resource.group_ref),
            status=str(data.get("status") or "active"),
            secret=str(data["key"]),
        )

    def create_key(self, resource: UpstreamResource) -> UpstreamKey:
        return self._create_named_key(
            resource,
            name=resource.marker,
            idempotency_key=f"reconcile-{resource.marker}",
        )

    def create_probe_key(self, resource: UpstreamResource) -> UpstreamKey:
        return self._create_named_key(
            resource,
            name=resource.probe_marker,
            idempotency_key=f"probe-{resource.probe_marker}",
        )

    def rename_key(self, key: UpstreamKey, name: str) -> None:
        self._request("PUT", f"/keys/{key.id}", body={"name": name})

    def enable_key(self, key: UpstreamKey) -> None:
        self._request("PUT", f"/keys/{key.id}", body={"status": "active"})

    def disable_key(self, key: UpstreamKey) -> None:
        self._request("PUT", f"/keys/{key.id}", body={"status": "inactive"})

    def delete_key(self, key_id: str) -> None:
        self._request("DELETE", f"/keys/{key_id}", allow_missing=True)


class NewAPIProvider(ProviderClient):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.api_base = str(config["api_base"]).rstrip("/")
        self.include_re = re.compile(str(config["include_group_regex"]), re.I)
        self.subscription_re = re.compile(
            str(config.get("subscription_group_regex") or r"(?:订阅|subscription|套餐|月卡|季卡|年卡)"),
            re.I,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": str(keychain_get(self.secret_account("cookie"))),
            "New-Api-User": str(keychain_get(self.secret_account("uid"))),
        }

    def login_with_credentials(self, account: str, password: str) -> None:
        response = self.session.post(
            f"{self.api_base}/api/user/login",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"username": account, "password": password},
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code in (400, 401, 403):
            raise ReconcileError(
                "interactive_auth_required",
                f"{self.provider_id} rejected the stored API login",
                next_action="check whether the password changed or the site added captcha or 2FA",
                context={"provider_id": self.provider_id},
            )
        _require_status(response)
        try:
            data = _response_data(response)
        except ReconcileError as exc:
            if exc.code in ("invalid_api_response", "upstream_api_error"):
                raise ReconcileError(
                    "interactive_auth_required",
                    f"{self.provider_id} rejected the stored API login",
                    next_action="check whether the password changed or the site added captcha or 2FA",
                    context={"provider_id": self.provider_id},
                ) from exc
            raise
        if not isinstance(data, dict):
            raise ReconcileError(
                "auth_required",
                f"{self.provider_id} login returned invalid user data",
                context={"provider_id": self.provider_id},
            )
        nested_user = data.get("user") if isinstance(data.get("user"), dict) else {}
        uid = data.get("id") or nested_user.get("id")
        cookie_header = "; ".join(
            f"{item.name}={item.value}" for item in self.session.cookies
        )
        if uid is None or not cookie_header:
            raise ReconcileError(
                "auth_required",
                f"{self.provider_id} login did not return a reusable session",
                context={"provider_id": self.provider_id},
            )
        keychain_set(self.secret_account("uid"), str(uid))
        keychain_set(self.secret_account("cookie"), cookie_header)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        allow_missing: bool = False,
        retryable: bool | None = None,
    ) -> Any:
        if retryable is None:
            retryable = method in ("GET", "PUT", "DELETE")
        response = _request_with_retries(
            self.session,
            method,
            f"{self.api_base}{path}",
            retryable=retryable,
            headers=self._headers(),
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code in (401, 403):
            self._login_from_keychain()
            response = _request_with_retries(
                self.session,
                method,
                f"{self.api_base}{path}",
                retryable=retryable,
                headers=self._headers(),
                json=body,
                timeout=DEFAULT_TIMEOUT,
            )
        if response.status_code in (401, 403):
            raise ReconcileError(
                "auth_required",
                f"{self.provider_id} API session could not be renewed",
                next_action="check whether the password changed or the site added captcha or 2FA",
                context={"provider_id": self.provider_id},
            )
        if allow_missing and response.status_code == 404:
            return None
        _require_status(response, (200, 201, 204) if allow_missing else (200, 201))
        if response.status_code == 204 or not response.content:
            return None
        try:
            return _response_data(response)
        except ReconcileError as exc:
            if exc.code != "invalid_api_response":
                raise
            try:
                observed = response.json()
            except ValueError:
                raise
            raise _schema_changed(
                self.provider_id,
                path,
                "standard JSON success envelope",
                observed,
            ) from exc

    def _list_keys(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        page = 1
        total = 1
        while len(items) < total:
            data = self._request("GET", f"/api/token/?p={page}&size=100")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise _schema_changed(
                    self.provider_id,
                    "/api/token/",
                    "object with items array and total integer",
                    data,
                )
            page_items = [item for item in data["items"] if isinstance(item, dict)]
            items.extend(page_items)
            total = int(data.get("total") or len(items))
            if not page_items:
                break
            page += 1
        if len(items) < total:
            raise ReconcileError("incomplete_scan", f"{self.provider_id} token scan stopped early")
        raw = {str(item.get("id")): item for item in items if item.get("id") is not None}
        return items, raw

    def scan(self) -> ProviderSnapshot:
        groups = self._request("GET", "/api/user/self/groups")
        raw_keys, raw_by_id = self._list_keys()
        if not isinstance(groups, dict):
            raise _schema_changed(
                self.provider_id,
                "/api/user/self/groups",
                "object keyed by group name",
                groups,
            )
        resources: list[UpstreamResource] = []
        subscription_allowlist = self.config.get("subscription_resource_allowlist")
        allowed_subscription_resources = (
            {str(value) for value in subscription_allowlist}
            if isinstance(subscription_allowlist, list)
            else None
        )
        for name, metadata in groups.items():
            if not self.include_re.search(str(name)):
                continue
            if not isinstance(metadata, dict) or "ratio" not in metadata:
                raise ReconcileError(
                    "unclassified_group",
                    f"{self.provider_id}/{name} has no authoritative ratio",
                    next_action="leave routing unchanged and retry on the next scheduled scan",
                    context={
                        "provider_id": self.provider_id,
                        "resource_id": f"group:{name}",
                    },
                )
            source_class = "subscription" if self.subscription_re.search(str(name)) else "metered"
            resource_id = f"group:{name}"
            if (
                source_class == "subscription"
                and allowed_subscription_resources is not None
                and resource_id not in allowed_subscription_resources
            ):
                continue
            multiplier = None if source_class == "subscription" else decimal_value(
                metadata["ratio"], field_name=f"{self.provider_id}/{name} multiplier"
            )
            resources.append(
                UpstreamResource(
                    provider_id=self.provider_id,
                    resource_id=resource_id,
                    group_ref=str(name),
                    group_name=str(name),
                    source_class=source_class,
                    multiplier=multiplier,
                )
            )
        keys = [
            UpstreamKey(
                id=str(item.get("id")),
                name=str(item.get("name") or ""),
                group_ref=str(item.get("group") or ""),
                status=str(item.get("status") or ""),
                secret=str(item.get("key") or ""),
            )
            for item in raw_keys
            if item.get("id") is not None
        ]
        return ProviderSnapshot(self.provider_id, resources, keys, raw_by_id)

    def _create_named_key(self, resource: UpstreamResource, *, name: str) -> UpstreamKey:
        payload = {
            "name": name,
            "remain_quota": 0,
            "expired_time": -1,
            "unlimited_quota": True,
            "model_limits_enabled": False,
            "model_limits": "",
            "allow_ips": "",
            "group": resource.group_ref,
            "cross_group_retry": False,
        }
        data = self._request("POST", "/api/token/", body=payload)
        if not isinstance(data, dict):
            # Some New-API forks return only success; resolve by stable marker.
            snapshot = self.scan()
            matches = snapshot.keys_by_name(name)
            if len(matches) != 1:
                raise ReconcileError("create_key_failed", f"{self.provider_id} created key cannot be resolved")
            return self.reveal_key(matches[0])
        key_id = data.get("id")
        if key_id is None:
            snapshot = self.scan()
            matches = snapshot.keys_by_name(name)
            if len(matches) != 1:
                raise ReconcileError("create_key_failed", f"{self.provider_id} created key cannot be resolved")
            return self.reveal_key(matches[0])
        key = UpstreamKey(
            id=str(key_id),
            name=str(data.get("name") or name),
            group_ref=str(data.get("group") or resource.group_ref),
            status=str(data.get("status") or "1"),
            secret=self._inference_secret(str(data.get("key") or "")),
        )
        return self.reveal_key(key)

    def create_key(self, resource: UpstreamResource) -> UpstreamKey:
        return self._create_named_key(resource, name=resource.marker)

    def create_probe_key(self, resource: UpstreamResource) -> UpstreamKey:
        return self._create_named_key(resource, name=resource.probe_marker)

    @staticmethod
    def _inference_secret(secret: str) -> str:
        if not secret or "*" in secret or secret.startswith("sk-"):
            return secret
        return "sk-" + secret

    def _full_key_payload(self, key_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/api/token/{key_id}")
        if not isinstance(data, dict):
            raise ReconcileError("invalid_api_response", f"{self.provider_id} token detail is invalid")
        return {
            field: data.get(field)
            for field in (
                "id",
                "name",
                "remain_quota",
                "expired_time",
                "unlimited_quota",
                "model_limits_enabled",
                "model_limits",
                "allow_ips",
                "group",
                "cross_group_retry",
            )
        }

    def rename_key(self, key: UpstreamKey, name: str) -> None:
        payload = self._full_key_payload(key.id)
        payload["name"] = name
        self._request("PUT", "/api/token/", body=payload)

    def enable_key(self, key: UpstreamKey) -> None:
        self._request("PUT", "/api/token/?status_only=true", body={"id": int(key.id), "status": 1})

    def disable_key(self, key: UpstreamKey) -> None:
        self._request("PUT", "/api/token/?status_only=true", body={"id": int(key.id), "status": 2})

    def delete_key(self, key_id: str) -> None:
        self._request("DELETE", f"/api/token/{key_id}/", allow_missing=True)

    def reveal_key(self, key: UpstreamKey) -> UpstreamKey:
        if key.secret and "*" not in key.secret:
            return key
        data = self._request("POST", f"/api/token/{key.id}/key", retryable=True)
        if isinstance(data, dict):
            secret = str(data.get("key") or "")
        else:
            secret = str(data or "")
        secret = self._inference_secret(secret)
        if not secret:
            raise ReconcileError("missing_key_secret", f"{self.provider_id} did not reveal key {key.id}")
        return UpstreamKey(key.id, key.name, key.group_ref, key.status, secret)


def provider_from_config(config: dict[str, Any]) -> ProviderClient:
    provider_type = config.get("type")
    if provider_type == "sub2api":
        return Sub2APIProvider(config)
    if provider_type == "new-api":
        return NewAPIProvider(config)
    raise ReconcileError("unsupported_provider", f"unsupported provider type: {provider_type}")


class TargetSub2API:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.api_base = str(config["api_base"]).rstrip("/")
        self.session = requests.Session()

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": str(keychain_get("target:admin_api_key")),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        retryable = (
            method == "GET"
            or idempotency_key is not None
            or path.endswith("/bulk-update")
            or path.endswith("/schedulable")
        )
        response = _request_with_retries(
            self.session,
            method,
            f"{self.api_base}{path}",
            retryable=retryable,
            headers=self._headers(idempotency_key=idempotency_key),
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        _require_status(response)
        return _response_data(response)

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        page = 1
        pages = 1
        while page <= pages:
            data = self._request(
                "GET",
                f"/admin/accounts?page={page}&page_size=100&sort_by=name&sort_order=asc&lite=1&include_scheduler_score=0",
            )
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ReconcileError("incomplete_scan", "target account pagination is invalid")
            accounts.extend(item for item in data["items"] if isinstance(item, dict))
            pages = int(data.get("pages") or 1)
            page += 1
        return accounts

    def get_settings(self) -> Any:
        return self._request("GET", "/admin/settings")

    def list_groups(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/admin/groups/all")
        if not isinstance(data, list):
            raise ReconcileError("invalid_api_response", "target group inventory is invalid")
        return [item for item in data if isinstance(item, dict)]

    def create_account(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        priority: int,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "platform": "openai",
            "type": "apikey",
            "credentials": {"base_url": base_url, "api_key": api_key},
            "extra": extra,
            "concurrency": int(self.config.get("concurrency") or 100),
            "priority": priority,
            # No group at creation prevents the brief default schedulable=true row
            # from receiving traffic before ownership and credentials are verified.
            "group_ids": [],
        }
        data = self._request(
            "POST",
            "/admin/accounts",
            body=body,
            idempotency_key="reconcile-create-" + name,
        )
        if not isinstance(data, dict) or data.get("id") is None:
            raise ReconcileError("create_account_failed", "target did not return the created account")
        return data

    def bulk_update(self, account_ids: list[int], **updates: Any) -> None:
        body = {"account_ids": account_ids, **updates}
        data = self._request("POST", "/admin/accounts/bulk-update", body=body)
        if not isinstance(data, dict):
            raise ReconcileError("target_update_failed", "target bulk update response is invalid")
        failed = data.get("failed_ids") or []
        if failed:
            raise ReconcileError("target_update_failed", "one or more target account updates failed")

    def set_schedulable(self, account_id: int, value: bool) -> None:
        self._request(
            "POST",
            f"/admin/accounts/{account_id}/schedulable",
            body={"schedulable": value},
        )


def enroll_sub2api_provider(config: dict[str, Any], *, cdp_host: str, cdp_port: int) -> None:
    origin = str(config["dashboard_origin"])
    page = find_edge_page(origin, host=cdp_host, port=cdp_port)
    values = cdp_evaluate(
        page,
        "({access_token: localStorage.getItem('auth_token'), refresh_token: localStorage.getItem('refresh_token')})",
    )
    if not isinstance(values, dict) or not values.get("access_token") or not values.get("refresh_token"):
        raise ReconcileError("auth_required", f"{config['id']} Edge login has no refreshable session")
    provider_id = str(config["id"])
    keychain_set(f"provider:{provider_id}:access_token", str(values["access_token"]))
    keychain_set(f"provider:{provider_id}:refresh_token", str(values["refresh_token"]))


def enroll_newapi_provider(config: dict[str, Any], *, cdp_host: str, cdp_port: int) -> None:
    origin = str(config["dashboard_origin"])
    page = find_edge_page(origin, host=cdp_host, port=cdp_port)
    uid = cdp_evaluate(page, "localStorage.getItem('uid')")
    host = urlparse(origin).hostname or ""
    relevant = [
        item
        for item in cdp_cookies(page, origin)
        if str(item.get("domain") or "").lstrip(".") == host and item.get("name") and item.get("value")
    ]
    cookie_header = "; ".join(f"{item['name']}={item['value']}" for item in relevant)
    if not uid or not cookie_header:
        raise ReconcileError("auth_required", f"{config['id']} Edge login has no reusable session")
    provider_id = str(config["id"])
    keychain_set(f"provider:{provider_id}:uid", str(uid))
    keychain_set(f"provider:{provider_id}:cookie", cookie_header)


def enroll_target(
    config: dict[str, Any], *, cdp_host: str, cdp_port: int, rotate_existing: bool = False
) -> None:
    origin = str(config["dashboard_origin"])
    page = find_edge_page(origin, host=cdp_host, port=cdp_port)
    token = cdp_evaluate(page, "localStorage.getItem('auth_token')")
    if not token:
        raise ReconcileError("auth_required", "target Edge admin session is missing")
    api_base = str(config["api_base"]).rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    status = requests.get(
        f"{api_base}/admin/settings/admin-api-key", headers=headers, timeout=DEFAULT_TIMEOUT
    )
    _require_status(status)
    status_data = _response_data(status)
    if isinstance(status_data, dict) and status_data.get("exists") and not rotate_existing:
        existing = keychain_get("target:admin_api_key", required=False)
        if existing:
            return
        raise ReconcileError(
            "target_admin_key_exists",
            "target already has an admin API key but it is not in this reconciler's Keychain",
            next_action="rerun enroll-edge with --rotate-target-admin-key",
        )
    response = requests.post(
        f"{api_base}/admin/settings/admin-api-key/regenerate",
        headers={**headers, "Content-Type": "application/json"},
        timeout=DEFAULT_TIMEOUT,
    )
    _require_status(response)
    data = _response_data(response)
    if not isinstance(data, dict) or not data.get("key"):
        raise ReconcileError("target_admin_key_failed", "target did not return the generated admin key")
    keychain_set("target:admin_api_key", str(data["key"]))


def parse_iso_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
