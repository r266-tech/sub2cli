from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .clients import (
    ProviderClient,
    ProviderSnapshot,
    TargetSub2API,
    enroll_newapi_provider,
    enroll_sub2api_provider,
    enroll_target,
    provider_from_config,
)
from .core import (
    METADATA_KEY,
    Action,
    Binding,
    ReconcileError,
    UpstreamKey,
    UpstreamResource,
    assign_priorities,
    decimal_text,
    desired_metadata,
    fingerprint_secret,
    marker_for,
    metadata_from_account,
    probe_marker_for,
)
from .store import (
    append_audit,
    atomic_write_json,
    default_config_path,
    default_state_dir,
    exclusive_lock,
    keychain_get,
    keychain_set,
    load_json,
    utc_now,
)


@dataclass
class Inventory:
    providers: dict[str, ProviderClient]
    snapshots: dict[str, ProviderSnapshot]
    target: TargetSub2API
    target_accounts: list[dict[str, Any]]
    bindings: list[Binding]
    settings: dict[str, Any]
    skipped_resources: list[dict[str, Any]] = dataclass_field(default_factory=list)


def resource_state_id(provider_id: str, resource_id: str) -> str:
    return f"{provider_id}/{resource_id}"


def _credential_origin(value: str) -> tuple[str, str, int] | None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def _require_same_credential_origin(
    label: str, dashboard_origin: str, api_base: str
) -> None:
    dashboard = _credential_origin(dashboard_origin)
    api = _credential_origin(api_base)
    if dashboard is None or api is None or dashboard != api:
        raise ReconcileError(
            "invalid_config",
            f"{label} dashboard_origin and api_base must use the same trusted origin",
        )
    if dashboard[0] == "http" and dashboard[1] not in ("127.0.0.1", "localhost", "::1"):
        raise ReconcileError(
            "invalid_config", f"{label} credentials require HTTPS outside loopback"
        )


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("version") != 1:
        raise ReconcileError("invalid_config", "config version must be 1")
    target = config.get("target")
    providers = config.get("providers")
    if not isinstance(target, dict) or not target.get("api_base") or not target.get("dashboard_origin"):
        raise ReconcileError("invalid_config", "target api_base and dashboard_origin are required")
    if not isinstance(target.get("group_id"), int):
        raise ReconcileError("invalid_config", "target group_id must be an integer")
    target_concurrency = target.get("concurrency", 100)
    if (
        isinstance(target_concurrency, bool)
        or not isinstance(target_concurrency, int)
        or target_concurrency < 1
    ):
        raise ReconcileError("invalid_config", "target.concurrency must be an integer >= 1")
    _require_same_credential_origin(
        "target", str(target["dashboard_origin"]), str(target["api_base"])
    )
    if not isinstance(providers, list) or not providers:
        raise ReconcileError("invalid_config", "at least one provider is required")
    delete_upstream = config.get("delete_upstream_keys", True)
    if not isinstance(delete_upstream, bool):
        raise ReconcileError("invalid_config", "delete_upstream_keys must be a boolean")
    try:
        delete_grace_hours = float(config.get("delete_grace_hours", 24))
    except (TypeError, ValueError) as exc:
        raise ReconcileError("invalid_config", "delete_grace_hours must be a number") from exc
    delete_confirmations = config.get("delete_min_confirmations", 2)
    if (
        isinstance(delete_confirmations, bool)
        or not isinstance(delete_confirmations, int)
        or delete_confirmations < 2
    ):
        raise ReconcileError(
            "invalid_config", "delete_min_confirmations must be an integer >= 2"
        )
    if delete_grace_hours <= 0 or not math.isfinite(delete_grace_hours):
        raise ReconcileError("invalid_config", "delete_grace_hours must be finite and > 0")
    notifications = config.get("notifications")
    if notifications is not None:
        if not isinstance(notifications, dict):
            raise ReconcileError("invalid_config", "notifications must be an object")
        command = notifications.get("telegram_command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(value, str) or not value for value in command)
        ):
            raise ReconcileError(
                "invalid_config", "notifications.telegram_command must be a non-empty string list"
            )
        codes = notifications.get("error_codes")
        if codes is not None and (
            not isinstance(codes, list)
            or not codes
            or any(not isinstance(value, str) or not value for value in codes)
        ):
            raise ReconcileError(
                "invalid_config", "notifications.error_codes must be a non-empty string list"
            )
        text_flag = notifications.get("telegram_text_flag", "--text")
        if text_flag is not None and (
            not isinstance(text_flag, str) or not text_flag
        ):
            raise ReconcileError(
                "invalid_config",
                "notifications.telegram_text_flag must be a non-empty string or null",
            )
        try:
            dedupe_hours = float(notifications.get("dedupe_hours", 24))
            timeout_seconds = float(notifications.get("timeout_seconds", 20))
        except (TypeError, ValueError) as exc:
            raise ReconcileError(
                "invalid_config", "notification timing values must be numeric"
            ) from exc
        if not math.isfinite(dedupe_hours) or dedupe_hours < 1:
            raise ReconcileError(
                "invalid_config", "notifications.dedupe_hours must be at least 1"
            )
        if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 120:
            raise ReconcileError(
                "invalid_config", "notifications.timeout_seconds must be between 1 and 120"
            )
    seen: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            raise ReconcileError("invalid_config", "provider config must be an object")
        provider_id = str(provider.get("id") or "")
        if not provider_id or provider_id in seen or not provider_id.replace("-", "").isalnum():
            raise ReconcileError("invalid_config", f"invalid or duplicate provider id: {provider_id}")
        seen.add(provider_id)
        if provider.get("type") not in ("sub2api", "new-api"):
            raise ReconcileError("invalid_config", f"unsupported provider type for {provider_id}")
        provider_concurrency = provider.get("target_concurrency", target_concurrency)
        if (
            isinstance(provider_concurrency, bool)
            or not isinstance(provider_concurrency, int)
            or provider_concurrency < 1
        ):
            raise ReconcileError(
                "invalid_config", f"{provider_id}.target_concurrency must be an integer >= 1"
            )
        for field in ("api_base", "dashboard_origin", "inference_base"):
            if not provider.get(field):
                raise ReconcileError("invalid_config", f"{provider_id}.{field} is required")
        _require_same_credential_origin(
            provider_id,
            str(provider["dashboard_origin"]),
            str(provider["api_base"]),
        )
        if provider.get("type") == "new-api" and not provider.get("include_group_regex"):
            raise ReconcileError("invalid_config", f"{provider_id}.include_group_regex is required")
        if "subscription_only" in provider:
            if provider.get("type") != "sub2api" or not isinstance(
                provider.get("subscription_only"), bool
            ):
                raise ReconcileError(
                    "invalid_config",
                    f"{provider_id}.subscription_only is supported only as a boolean for sub2api",
                )
        if "exclude_group_ids" in provider:
            excluded = provider.get("exclude_group_ids")
            if provider.get("type") != "sub2api" or not isinstance(excluded, list):
                raise ReconcileError(
                    "invalid_config",
                    f"{provider_id}.exclude_group_ids is supported only as a list for sub2api",
                )
            if any(
                isinstance(value, bool) or not isinstance(value, (int, str))
                for value in excluded
            ):
                raise ReconcileError(
                    "invalid_config", f"{provider_id}.exclude_group_ids has an invalid id"
                )
        subscription_allowlist = provider.get("subscription_resource_allowlist")
        if subscription_allowlist is None:
            raise ReconcileError(
                "invalid_config",
                f"{provider_id}.subscription_resource_allowlist is required; use [] to reject all subscriptions",
            )
        if (
            not isinstance(subscription_allowlist, list)
            or any(
                not isinstance(value, str) or not value.startswith("group:")
                for value in subscription_allowlist
            )
            or len(set(subscription_allowlist)) != len(subscription_allowlist)
        ):
            raise ReconcileError(
                "invalid_config",
                f"{provider_id}.subscription_resource_allowlist must contain unique group:* strings",
            )
        if provider.get("type") == "new-api" and subscription_allowlist:
            raise ReconcileError(
                "invalid_config",
                f"{provider_id} cannot enable subscriptions without an authoritative subscription status adapter",
            )
        if (
            provider.get("type") == "sub2api"
            and subscription_allowlist
            and provider.get("require_subscription_expiry") is not True
        ):
            raise ReconcileError(
                "invalid_config",
                f"{provider_id}.require_subscription_expiry must be true when subscriptions are allowlisted",
            )
        if "require_subscription_expiry" in provider and (
            provider.get("type") != "sub2api"
            or not isinstance(provider.get("require_subscription_expiry"), bool)
        ):
            raise ReconcileError(
                "invalid_config",
                f"{provider_id}.require_subscription_expiry is supported only as a boolean for sub2api",
            )
        if "probe_new_resources" in provider and not isinstance(
            provider.get("probe_new_resources"), bool
        ):
            raise ReconcileError(
                "invalid_config", f"{provider_id}.probe_new_resources must be a boolean"
            )
        try:
            probe_timeout = float(provider.get("probe_timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise ReconcileError(
                "invalid_config", f"{provider_id}.probe_timeout_seconds must be a number"
            ) from exc
        if not math.isfinite(probe_timeout) or not 1 <= probe_timeout <= 120:
            raise ReconcileError(
                "invalid_config",
                f"{provider_id}.probe_timeout_seconds must be between 1 and 120",
            )
        for regex_field in ("probe_model_allow_regex", "probe_model_deny_regex"):
            if regex_field not in provider:
                continue
            try:
                re.compile(str(provider[regex_field]))
            except re.error as exc:
                raise ReconcileError(
                    "invalid_config", f"{provider_id}.{regex_field} is invalid"
                ) from exc
        preferred_models = provider.get("probe_preferred_models")
        if preferred_models is not None and (
            not isinstance(preferred_models, list)
            or not preferred_models
            or any(not isinstance(value, str) or not value.strip() for value in preferred_models)
        ):
            raise ReconcileError(
                "invalid_config",
                f"{provider_id}.probe_preferred_models must be a non-empty string list",
            )
        adoption_resources: set[str] = set()
        for adoption in provider.get("adopt", []):
            if not isinstance(adoption, dict) or not adoption.get("resource_id"):
                raise ReconcileError("invalid_config", f"{provider_id} has an invalid adoption entry")
            resource_id = str(adoption["resource_id"])
            if resource_id in adoption_resources:
                raise ReconcileError("invalid_config", f"{provider_id}/{resource_id} is adopted twice")
            adoption_resources.add(resource_id)
            if adoption.get("key_id") is None or adoption.get("account_id") is None:
                raise ReconcileError("invalid_config", f"{provider_id}/{resource_id} adoption needs key_id and account_id")
    return config


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or default_config_path()
    config = load_json(path, None)
    if not isinstance(config, dict):
        raise ReconcileError(
            "config_missing",
            f"reconciler config is missing at {path}",
            next_action="create the private provider inventory, then run enroll-edge",
        )
    return validate_config(config)


def _target_concurrency(config: dict[str, Any], provider_id: str) -> int:
    default = int((config.get("target") or {}).get("concurrency", 100))
    provider = next(
        (item for item in config.get("providers", []) if item.get("id") == provider_id),
        None,
    )
    if provider is None:
        raise ReconcileError("invalid_config", f"provider config is missing for {provider_id}")
    return int(provider.get("target_concurrency", default))


def _adoption_map(provider_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["resource_id"]): item for item in provider_config.get("adopt", [])}


def _probe_policy_hash(provider_config: dict[str, Any]) -> str:
    payload = {
        "policy": "codex-responses-sse-v1",
        "inference_base": str(provider_config.get("inference_base") or "").rstrip("/"),
        "allow": str(provider_config.get("probe_model_allow_regex") or r"^(?:gpt-|codex-)"),
        "deny": str(
            provider_config.get("probe_model_deny_regex")
            or r"(?:^|[-_/])(?:audio|embedding|image|realtime|speech|transcribe|tts|video)(?:$|[-_/])"
        ),
        "preferred": provider_config.get("probe_preferred_models")
        or ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex-spark"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_inference_base(provider_config: dict[str, Any]) -> str:
    return str(provider_config.get("inference_base") or "").strip().rstrip("/")


def _resource_is_grandfathered(
    provider_config: dict[str, Any],
    resource: UpstreamResource,
    state: dict[str, Any],
) -> bool:
    if resource.resource_id in _adoption_map(provider_config):
        return True
    state_resources = state.get("resources")
    entry = (
        state_resources.get(resource_state_id(resource.provider_id, resource.resource_id))
        if isinstance(state_resources, dict)
        else None
    )
    return bool(
        isinstance(entry, dict)
        and entry.get("status") == "active"
        and entry.get("upstream_key_id") is not None
        and entry.get("target_account_id") is not None
    )


def _candidate_probe_record(state: dict[str, Any], state_id: str) -> dict[str, Any] | None:
    records = state.get("candidate_probes")
    value = records.get(state_id) if isinstance(records, dict) else None
    return value if isinstance(value, dict) else None


def _probe_record_matches_resource(
    provider_config: dict[str, Any],
    resource: UpstreamResource,
    record: dict[str, Any],
) -> bool:
    return bool(
        record.get("outcome") == "compatible"
        and record.get("policy_hash") == _probe_policy_hash(provider_config)
        and record.get("inference_base") == _normalized_inference_base(provider_config)
        and str(record.get("group_ref") or "") == resource.group_ref
        and record.get("marker") == resource.marker
        and record.get("upstream_key_id") is not None
        and isinstance(record.get("key_fingerprint"), str)
        and bool(record.get("key_fingerprint"))
    )


def _probe_record_matches_live_key(
    resource: UpstreamResource,
    record: dict[str, Any],
    snapshot: ProviderSnapshot,
) -> bool:
    key = snapshot.key_by_id(record.get("upstream_key_id"))
    if (
        key is None
        or key.name != resource.marker
        or key.group_ref != resource.group_ref
        or key.status not in ("active", "1", "enabled")
    ):
        return False
    secret = key.secret if key.secret and "*" not in key.secret else None
    if not secret:
        secret = keychain_get(_keychain_resource_account(resource), required=False)
    if not secret:
        return False
    return fingerprint_secret(secret) == record.get("key_fingerprint")


def _resource_probe_gate(
    provider_config: dict[str, Any],
    resource: UpstreamResource,
    state: dict[str, Any],
    *,
    snapshot: ProviderSnapshot | None = None,
) -> tuple[bool, str | None]:
    if not provider_config.get("probe_new_resources", False):
        return True, None
    if _resource_is_grandfathered(provider_config, resource, state):
        return True, None
    state_id = resource_state_id(resource.provider_id, resource.resource_id)
    record = _candidate_probe_record(state, state_id)
    if record and _probe_record_matches_resource(provider_config, resource, record):
        if snapshot is not None and not _probe_record_matches_live_key(
            resource, record, snapshot
        ):
            return False, "probe_key_changed"
        return True, None
    return False, str((record or {}).get("code") or "probe_required")


def _safe_setting_bool(settings: dict[str, Any], key: str) -> bool | None:
    if key not in settings:
        return None
    value = settings[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        if value.lower() in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _validate_target(config: dict[str, Any], target: TargetSub2API) -> dict[str, Any]:
    raw_settings = target.get_settings()
    if not isinstance(raw_settings, dict):
        raise ReconcileError("invalid_api_response", "target settings response is invalid")
    if config.get("target", {}).get("require_legacy_scheduler", True):
        advanced = _safe_setting_bool(raw_settings, "openai_advanced_scheduler_enabled")
        if advanced is None:
            raise ReconcileError("scheduler_unknown", "cannot verify target OpenAI scheduler mode")
        if advanced:
            raise ReconcileError(
                "scheduler_incompatible",
                "target advanced OpenAI scheduler is enabled, so strict priority tiers are not guaranteed",
                next_action="disable the advanced scheduler or add a strict tier scheduler before applying",
            )
    group_id = int(config["target"]["group_id"])
    groups = target.list_groups()
    match = next((item for item in groups if int(item.get("id") or -1) == group_id), None)
    if not match or match.get("status") != "active":
        raise ReconcileError("target_group_missing", f"target group {group_id} is not active")
    return raw_settings


def _choose_key(
    resource: UpstreamResource,
    snapshot: ProviderSnapshot,
    adoption: dict[str, Any] | None,
    state_entry: dict[str, Any] | None,
) -> UpstreamKey | None:
    marker_matches = snapshot.keys_by_name(resource.marker)
    if len(marker_matches) > 1:
        raise ReconcileError("duplicate_marker", f"multiple upstream keys use marker {resource.marker}")
    candidates: list[UpstreamKey] = marker_matches[:]
    for key_id in (
        adoption.get("key_id") if adoption else None,
        state_entry.get("upstream_key_id") if state_entry else None,
    ):
        candidate = snapshot.key_by_id(key_id)
        if candidate and all(item.id != candidate.id for item in candidates):
            candidates.append(candidate)
    distinct = {item.id for item in candidates}
    if len(distinct) > 1:
        raise ReconcileError("ownership_conflict", f"conflicting upstream key ownership for {resource.marker}")
    if not candidates:
        return None
    key = candidates[0]
    if key.group_ref != resource.group_ref:
        raise ReconcileError("group_drift", f"managed key {key.id} is bound to the wrong upstream group")
    return key


def _managed_accounts_by_resource(accounts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        metadata = metadata_from_account(account)
        if not metadata:
            continue
        provider_id = metadata.get("provider_id")
        resource_id = metadata.get("resource_id")
        if provider_id and resource_id:
            result.setdefault(resource_state_id(str(provider_id), str(resource_id)), []).append(account)
    return result


def _choose_target_account(
    resource: UpstreamResource,
    accounts_by_id: dict[int, dict[str, Any]],
    managed_accounts: dict[str, list[dict[str, Any]]],
    adoption: dict[str, Any] | None,
    state_entry: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool]:
    state_id = resource_state_id(resource.provider_id, resource.resource_id)
    marker_matches = managed_accounts.get(state_id, [])
    if len(marker_matches) > 1:
        raise ReconcileError("duplicate_target_marker", f"multiple target accounts own {state_id}")
    candidates = marker_matches[:]
    adopted = False
    for source, account_id in (
        ("adoption", adoption.get("account_id") if adoption else None),
        ("state", state_entry.get("target_account_id") if state_entry else None),
    ):
        if account_id is None:
            continue
        candidate = accounts_by_id.get(int(account_id))
        if not candidate:
            if source == "adoption":
                raise ReconcileError("adoption_missing", f"adopted target account {account_id} does not exist")
            continue
        if all(int(item["id"]) != int(candidate["id"]) for item in candidates):
            candidates.append(candidate)
        adopted = adopted or source == "adoption"
    if len({int(item["id"]) for item in candidates}) > 1:
        raise ReconcileError("ownership_conflict", f"conflicting target account ownership for {state_id}")
    if not candidates:
        return None, False
    account = candidates[0]
    if account.get("platform") != "openai" or account.get("type") != "apikey":
        raise ReconcileError("target_type_mismatch", f"target account {account['id']} is not an OpenAI API key")
    return account, adopted and not marker_matches


def _seed_inactive_adoptions(
    provider_config: dict[str, Any],
    snapshot: ProviderSnapshot,
    accounts_by_id: dict[int, dict[str, Any]],
    state_resources: dict[str, Any],
) -> None:
    active_ids = {item.resource_id for item in snapshot.resources}
    provider_id = str(provider_config["id"])
    for adoption in provider_config.get("adopt", []):
        resource_id = str(adoption["resource_id"])
        if resource_id in active_ids:
            continue
        state_id = resource_state_id(provider_id, resource_id)
        existing = state_resources.get(state_id)
        key = snapshot.key_by_id(adoption.get("key_id"))
        account = accounts_by_id.get(int(adoption["account_id"]))
        if key is None and not isinstance(existing, dict):
            raise ReconcileError(
                "adoption_missing",
                f"adopted upstream key {adoption.get('key_id')} does not exist for {provider_id}",
            )
        if account is None:
            raise ReconcileError(
                "adoption_missing",
                f"adopted target account {adoption.get('account_id')} does not exist",
            )
        if key is None:
            continue
        expected_marker = marker_for(provider_id, resource_id)
        if not isinstance(existing, dict) and key.name != expected_marker:
            raise ReconcileError(
                "inactive_adoption_unclaimed",
                f"inactive adopted key {key.id} for {provider_id}/{resource_id} has no ownership marker",
                next_action="remove the inactive adoption or explicitly establish ownership outside the scheduled workflow",
            )
        state_resources.setdefault(
            state_id,
            {
                "provider_id": provider_id,
                "resource_id": resource_id,
                "marker": expected_marker,
                "upstream_key_id": key.id,
                "target_account_id": int(account["id"]),
                "key_fingerprint": key.fingerprint if key.secret else None,
                "source_class": "unknown_inactive",
                "multiplier": None,
                "priority": account.get("priority"),
                "missing_since": None,
                "missing_count": 0,
                "status": "bootstrap_inactive",
            },
        )


def _single_named_key(
    snapshot: ProviderSnapshot, name: str, *, label: str
) -> UpstreamKey | None:
    matches = snapshot.keys_by_name(name)
    if len(matches) > 1:
        raise ReconcileError(
            "duplicate_probe_marker", f"multiple upstream keys use {label} marker"
        )
    return matches[0] if matches else None


def _seed_candidate_probe_states(state: dict[str, Any]) -> None:
    records = state.setdefault("candidate_probes", {})
    state_resources = state.setdefault("resources", {})
    if not isinstance(records, dict):
        raise ReconcileError("invalid_local_state", "state.candidate_probes is invalid")
    if not isinstance(state_resources, dict):
        raise ReconcileError("invalid_local_state", "state.resources is invalid")
    for state_id, record in records.items():
        if state_id in state_resources or not isinstance(record, dict):
            continue
        provider_id = str(record.get("provider_id") or state_id.partition("/")[0])
        resource_id = str(record.get("resource_id") or state_id.partition("/")[2])
        key_id = record.get("upstream_key_id")
        if not provider_id or not resource_id or key_id is None:
            continue
        compatible = record.get("outcome") == "compatible"
        pending = record.get("outcome") == "pending"
        marker = str(record.get("marker") or "")
        if not marker:
            marker = (
                marker_for(provider_id, resource_id)
                if compatible
                else probe_marker_for(provider_id, resource_id)
            )
        state_resources[state_id] = {
            "provider_id": provider_id,
            "resource_id": resource_id,
            "marker": marker,
            "upstream_key_id": str(key_id),
            "target_account_id": None,
            "key_fingerprint": record.get("key_fingerprint"),
            "source_class": "unknown_candidate",
            "multiplier": None,
            "priority": None,
            "missing_since": None,
            "missing_count": 0,
            "status": (
                "probe_compatible"
                if compatible
                else "probe_pending" if pending else "probe_deferred"
            ),
            "probe": dict(record),
        }


def _write_probe_pending_state(
    records: dict[str, Any],
    state_resources: dict[str, Any],
    provider_config: dict[str, Any],
    resource: UpstreamResource,
    key: UpstreamKey,
    *,
    attempt_count: int,
    attempted_at: str,
) -> dict[str, Any]:
    state_id = resource_state_id(resource.provider_id, resource.resource_id)
    previous = state_resources.get(state_id)
    previous = previous if isinstance(previous, dict) else {}
    key_fingerprint = (
        key.fingerprint if key.secret and "*" not in key.secret else None
    )
    record = {
        "provider_id": resource.provider_id,
        "resource_id": resource.resource_id,
        "policy_hash": _probe_policy_hash(provider_config),
        "inference_base": _normalized_inference_base(provider_config),
        "outcome": "pending",
        "code": "probe_pending",
        "retryable": True,
        "http_status": None,
        "model": None,
        "model_count": 0,
        "upstream_key_id": key.id,
        "key_fingerprint": key_fingerprint,
        "group_ref": resource.group_ref,
        "marker": key.name,
        "probe_marker": resource.probe_marker,
        "attempt_count": attempt_count,
        "last_attempt_at": attempted_at,
    }
    records[state_id] = record
    state_resources[state_id] = {
        "provider_id": resource.provider_id,
        "resource_id": resource.resource_id,
        "marker": key.name,
        "upstream_key_id": key.id,
        "target_account_id": previous.get("target_account_id"),
        "key_fingerprint": key_fingerprint,
        "source_class": resource.source_class,
        "multiplier": decimal_text(resource.multiplier),
        "priority": previous.get("priority"),
        "missing_since": None,
        "missing_count": 0,
        "status": "probe_pending",
        "probe": dict(record),
    }
    return record


def _pending_mutation_events(pending: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    chain = pending.get("recovery_chain")
    if isinstance(chain, list):
        for item in chain:
            mutations = item.get("mutations") if isinstance(item, dict) else None
            if isinstance(mutations, list):
                events.extend(event for event in mutations if isinstance(event, dict))
    mutations = pending.get("mutations")
    if isinstance(mutations, list):
        events.extend(event for event in mutations if isinstance(event, dict))
    return events


def _recover_pending_probe_keys(
    config: dict[str, Any],
    state: dict[str, Any],
    previous_pending: dict[str, Any] | None,
    *,
    audit_path: Path,
    persist_state: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    if not isinstance(previous_pending, dict):
        return []
    state_resources = state.setdefault("resources", {})
    records = state.setdefault("candidate_probes", {})
    if not isinstance(state_resources, dict) or not isinstance(records, dict):
        raise ReconcileError("invalid_local_state", "candidate recovery state is invalid")
    provider_configs = {str(item["id"]): item for item in config["providers"]}
    intents: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in _pending_mutation_events(previous_pending):
        if event.get("phase") != "intent" or event.get("kind") != "create_probe_key":
            continue
        provider_id = str(event.get("provider_id") or "")
        resource_id = str(event.get("resource_id") or "")
        marker = str(event.get("marker") or "")
        if (
            provider_id not in provider_configs
            or not resource_id.startswith("group:")
            or marker != probe_marker_for(provider_id, resource_id)
        ):
            raise ReconcileError(
                "invalid_pending_recovery",
                "pending probe-key recovery intent is invalid",
            )
        intents[(provider_id, resource_id, marker)] = event
    if not intents:
        return []

    snapshots: dict[str, ProviderSnapshot] = {}
    recovered: list[dict[str, Any]] = []
    for (provider_id, resource_id, marker), event in sorted(intents.items()):
        provider_config = provider_configs[provider_id]
        snapshot = snapshots.get(provider_id)
        if snapshot is None:
            snapshot = provider_from_config(provider_config).scan()
            snapshots[provider_id] = snapshot
        key = _single_named_key(snapshot, marker, label="recovery probe")
        if key is None:
            continue
        group_ref = resource_id.removeprefix("group:")
        if key.group_ref != group_ref:
            raise ReconcileError(
                "ownership_conflict",
                f"recovered probe key {key.id} is bound to the wrong upstream group",
            )
        state_id = resource_state_id(provider_id, resource_id)
        previous = state_resources.get(state_id)
        previous = previous if isinstance(previous, dict) else {}
        if (
            str(previous.get("upstream_key_id") or "") == key.id
            and previous.get("marker") == marker
        ):
            continue
        resource = next(
            (item for item in snapshot.resources if item.resource_id == resource_id),
            None,
        )
        attempted_at = str(event.get("at") or utc_now())
        if resource is not None:
            record = _write_probe_pending_state(
                records,
                state_resources,
                provider_config,
                resource,
                key,
                attempt_count=0,
                attempted_at=attempted_at,
            )
        else:
            key_fingerprint = (
                key.fingerprint if key.secret and "*" not in key.secret else None
            )
            record = {
                "provider_id": provider_id,
                "resource_id": resource_id,
                "policy_hash": _probe_policy_hash(provider_config),
                "inference_base": _normalized_inference_base(provider_config),
                "outcome": "pending",
                "code": "probe_recovery_pending",
                "retryable": True,
                "http_status": None,
                "model": None,
                "model_count": 0,
                "upstream_key_id": key.id,
                "key_fingerprint": key_fingerprint,
                "group_ref": group_ref,
                "marker": marker,
                "probe_marker": marker,
                "attempt_count": 0,
                "last_attempt_at": attempted_at,
            }
            records[state_id] = record
            state_resources[state_id] = {
                "provider_id": provider_id,
                "resource_id": resource_id,
                "marker": marker,
                "upstream_key_id": key.id,
                "target_account_id": previous.get("target_account_id"),
                "key_fingerprint": key_fingerprint,
                "source_class": previous.get("source_class", "unknown_candidate"),
                "multiplier": previous.get("multiplier"),
                "priority": previous.get("priority"),
                "missing_since": None,
                "missing_count": 0,
                "status": "probe_pending",
                "probe": dict(record),
            }
        persist_state(state)
        safe_result = {
            "provider_id": provider_id,
            "resource_id": resource_id,
            "upstream_key_id": key.id,
            "group_present": resource is not None,
        }
        recovered.append(safe_result)
        append_audit(
            audit_path,
            {"event": "candidate_probe_key_recovered", **safe_result},
        )
    return recovered


def _write_candidate_resource_state(
    state_resources: dict[str, Any],
    resource: UpstreamResource,
    key: UpstreamKey,
    record: dict[str, Any],
    *,
    compatible: bool,
) -> None:
    state_id = resource_state_id(resource.provider_id, resource.resource_id)
    previous = state_resources.get(state_id)
    previous = previous if isinstance(previous, dict) else {}
    state_resources[state_id] = {
        "provider_id": resource.provider_id,
        "resource_id": resource.resource_id,
        "marker": key.name,
        "upstream_key_id": key.id,
        "target_account_id": previous.get("target_account_id"),
        "key_fingerprint": key.fingerprint,
        "source_class": resource.source_class,
        "multiplier": decimal_text(resource.multiplier),
        "priority": previous.get("priority"),
        "missing_since": None,
        "missing_count": 0,
        "status": "probe_compatible" if compatible else "probe_deferred",
        "probe": dict(record),
    }


def _qualify_pending_resources(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    audit_path: Path,
    record_mutation: Callable[[dict[str, Any]], None] | None = None,
    persist_state: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    record_mutation = record_mutation or (lambda _event: None)
    persist_state = persist_state or (lambda _state: None)
    _seed_candidate_probe_states(state)
    records = state.setdefault("candidate_probes", {})
    if not isinstance(records, dict):
        raise ReconcileError("invalid_local_state", "state.candidate_probes is invalid")
    state_resources = state.get("resources")
    if not isinstance(state_resources, dict):
        raise ReconcileError("invalid_local_state", "state.resources is invalid")
    results: list[dict[str, Any]] = []
    for provider_config in config["providers"]:
        if not provider_config.get("probe_new_resources", False):
            continue
        client = provider_from_config(provider_config)
        snapshot = client.scan()
        policy_hash = _probe_policy_hash(provider_config)
        for resource in snapshot.resources:
            state_id = resource_state_id(resource.provider_id, resource.resource_id)
            if _resource_is_grandfathered(provider_config, resource, state):
                records.pop(state_id, None)
                continue
            cached = _candidate_probe_record(state, state_id)
            formal_key = _single_named_key(
                snapshot, resource.marker, label="managed"
            )
            probe_key = _single_named_key(
                snapshot, resource.probe_marker, label="probe"
            )
            if formal_key is not None and probe_key is not None:
                # An interrupted prior promotion can leave only one official
                # key; an extra probe marker is ours and has never reached Relay.
                record_mutation(
                    {
                        "phase": "intent",
                        "kind": "delete_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": probe_key.id,
                    }
                )
                client.delete_key(probe_key.id)
                record_mutation(
                    {
                        "phase": "done",
                        "kind": "delete_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": probe_key.id,
                    }
                )
                probe_key = None
            qualified, _ = _resource_probe_gate(
                provider_config,
                resource,
                state,
                snapshot=snapshot,
            )
            if qualified:
                continue
            key = formal_key or probe_key
            created_key = False
            enabled_key = False
            if key is None:
                record_mutation(
                    {
                        "phase": "intent",
                        "kind": "create_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "marker": resource.probe_marker,
                    }
                )
                key = client.create_probe_key(resource)
                created_key = True
            elif key.status not in ("active", "1", "enabled"):
                record_mutation(
                    {
                        "phase": "intent",
                        "kind": "enable_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": key.id,
                    }
                )
                client.enable_key(key)
                key.status = "active"
                enabled_key = True

            previous_attempts = int((cached or {}).get("attempt_count") or 0)
            attempt_count = previous_attempts + 1
            attempted_at = utc_now()
            # State is the cleanup handle if the group disappears. Persist it
            # before recording a completed create/enable mutation.
            pending_record = _write_probe_pending_state(
                records,
                state_resources,
                provider_config,
                resource,
                key,
                attempt_count=attempt_count,
                attempted_at=attempted_at,
            )
            persist_state(state)

            if created_key:
                record_mutation(
                    {
                        "phase": "done",
                        "kind": "create_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "marker": resource.probe_marker,
                        "upstream_key_id": key.id,
                    }
                )
            elif enabled_key:
                record_mutation(
                    {
                        "phase": "done",
                        "kind": "enable_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": key.id,
                    }
                )

            revealed_key = False
            if not key.secret or "*" in key.secret:
                cached_secret = keychain_get(
                    _keychain_resource_account(resource), required=False
                )
                if (
                    cached_secret
                    and cached
                    and str(cached.get("upstream_key_id") or "") == key.id
                    and cached.get("key_fingerprint")
                    == fingerprint_secret(cached_secret)
                ):
                    key = UpstreamKey(
                        key.id,
                        key.name,
                        key.group_ref,
                        key.status,
                        cached_secret,
                    )
                else:
                    record_mutation(
                        {
                            "phase": "intent",
                            "kind": "reveal_probe_key",
                            "provider_id": resource.provider_id,
                            "resource_id": resource.resource_id,
                            "upstream_key_id": key.id,
                        }
                    )
                    key = client.reveal_key(key)
                    revealed_key = True
            keychain_set(_keychain_resource_account(resource), key.secret)
            # Bind the recovered secret to this exact key before any probe can
            # fail, without ever persisting the secret itself.
            if (
                key.secret
                and "*" not in key.secret
                and pending_record.get("key_fingerprint") != key.fingerprint
            ):
                pending_record = _write_probe_pending_state(
                    records,
                    state_resources,
                    provider_config,
                    resource,
                    key,
                    attempt_count=attempt_count,
                    attempted_at=attempted_at,
                )
                persist_state(state)
            if revealed_key:
                record_mutation(
                    {
                        "phase": "done",
                        "kind": "reveal_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": key.id,
                    }
                )

            record_mutation(
                {
                    "phase": "intent",
                    "kind": "probe_upstream_resource",
                    "provider_id": resource.provider_id,
                    "resource_id": resource.resource_id,
                    "upstream_key_id": key.id,
                }
            )
            probe = client.probe_resource(resource, key)
            record_mutation(
                {
                    "phase": "done",
                    "kind": "probe_upstream_resource",
                    "provider_id": resource.provider_id,
                    "resource_id": resource.resource_id,
                    "upstream_key_id": key.id,
                    "compatible": probe.compatible,
                    "code": probe.code,
                }
            )
            if probe.compatible:
                if key.name != resource.marker:
                    record_mutation(
                        {
                            "phase": "intent",
                            "kind": "promote_probe_key",
                            "provider_id": resource.provider_id,
                            "resource_id": resource.resource_id,
                            "upstream_key_id": key.id,
                            "to": resource.marker,
                        }
                    )
                    client.rename_key(key, resource.marker)
                    key.name = resource.marker
                    record_mutation(
                        {
                            "phase": "done",
                            "kind": "promote_probe_key",
                            "provider_id": resource.provider_id,
                            "resource_id": resource.resource_id,
                            "upstream_key_id": key.id,
                            "to": resource.marker,
                        }
                    )
            else:
                record_mutation(
                    {
                        "phase": "intent",
                        "kind": "disable_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": key.id,
                    }
                )
                client.disable_key(key)
                key.status = "inactive"
                record_mutation(
                    {
                        "phase": "done",
                        "kind": "disable_probe_key",
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "upstream_key_id": key.id,
                    }
                )
            record = {
                "provider_id": resource.provider_id,
                "resource_id": resource.resource_id,
                "policy_hash": policy_hash,
                "inference_base": _normalized_inference_base(provider_config),
                "outcome": "compatible" if probe.compatible else "retry",
                "code": probe.code,
                "retryable": probe.retryable,
                "http_status": probe.http_status,
                "model": probe.model,
                "model_count": probe.model_count,
                "upstream_key_id": key.id,
                "key_fingerprint": key.fingerprint,
                "group_ref": resource.group_ref,
                "marker": key.name,
                "probe_marker": resource.probe_marker,
                "attempt_count": attempt_count,
                "last_attempt_at": utc_now(),
            }
            records[state_id] = record
            _write_candidate_resource_state(
                state_resources,
                resource,
                key,
                record,
                compatible=probe.compatible,
            )
            safe_result = {
                "provider_id": resource.provider_id,
                "resource_id": resource.resource_id,
                **probe.safe_dict(),
            }
            results.append(safe_result)
            append_audit(
                audit_path,
                {
                    "event": "candidate_probe_completed",
                    **safe_result,
                },
            )
    return results


def build_inventory(config: dict[str, Any], state: dict[str, Any]) -> Inventory:
    _seed_candidate_probe_states(state)
    target = TargetSub2API(config["target"])
    settings = _validate_target(config, target)
    target_accounts = target.list_accounts()
    accounts_by_id = {int(item["id"]): item for item in target_accounts if item.get("id") is not None}
    managed_accounts = _managed_accounts_by_resource(target_accounts)

    providers: dict[str, ProviderClient] = {}
    snapshots: dict[str, ProviderSnapshot] = {}
    resources: list[UpstreamResource] = []
    skipped_resources: list[dict[str, Any]] = []
    provider_configs = {str(item["id"]): item for item in config["providers"]}
    state_resources = state.setdefault("resources", {})
    if not isinstance(state_resources, dict):
        raise ReconcileError("invalid_local_state", "state.resources is invalid")

    for provider_config in config["providers"]:
        client = provider_from_config(provider_config)
        snapshot = client.scan()
        providers[client.provider_id] = client
        snapshots[client.provider_id] = snapshot
        _seed_inactive_adoptions(provider_config, snapshot, accounts_by_id, state_resources)
        adoption_map = _adoption_map(provider_config)
        for resource in snapshot.resources:
            state_id = resource_state_id(resource.provider_id, resource.resource_id)
            entry = state_resources.get(state_id)
            qualified, reason = _resource_probe_gate(
                provider_config,
                resource,
                state,
                snapshot=snapshot,
            )
            if not qualified:
                skipped_resources.append(
                    {
                        "provider_id": resource.provider_id,
                        "resource_id": resource.resource_id,
                        "group_name": resource.group_name,
                        "reason": reason,
                        "target_account_id": (
                            entry.get("target_account_id")
                            if isinstance(entry, dict)
                            else None
                        ),
                    }
                )
                continue
            adoption = adoption_map.get(resource.resource_id)
            resource.key = _choose_key(resource, snapshot, adoption, entry)
            if resource.key:
                trusted_key_ids = {
                    str(value)
                    for value in (
                        adoption.get("key_id") if adoption else None,
                        entry.get("upstream_key_id") if isinstance(entry, dict) else None,
                    )
                    if value is not None
                }
                cached_secret = (
                    keychain_get(_keychain_resource_account(resource), required=False)
                    if resource.key.id in trusted_key_ids
                    else None
                )
                if cached_secret:
                    resource.key = UpstreamKey(
                        resource.key.id,
                        resource.key.name,
                        resource.key.group_ref,
                        resource.key.status,
                        cached_secret,
                    )
                else:
                    resource.key = client.reveal_key(resource.key)
            resources.append(resource)

    if resources:
        resources = assign_priorities(resources)
    elif not skipped_resources and not state_resources:
        resources = assign_priorities(resources)
    bindings: list[Binding] = []
    for resource in resources:
        provider_config = provider_configs[resource.provider_id]
        adoption = _adoption_map(provider_config).get(resource.resource_id)
        entry = state_resources.get(resource_state_id(resource.provider_id, resource.resource_id))
        account, adopted = _choose_target_account(
            resource, accounts_by_id, managed_accounts, adoption, entry
        )
        bindings.append(Binding(resource, account, adopted=adopted))
    return Inventory(
        providers,
        snapshots,
        target,
        target_accounts,
        bindings,
        settings,
        skipped_resources,
    )


def _missing_delete_eligible(
    config: dict[str, Any],
    entry: dict[str, Any],
    *,
    projected_count: int,
    now: datetime,
) -> bool:
    missing_since = _parse_when(entry.get("missing_since"))
    if missing_since is None:
        return False
    grace = timedelta(hours=float(config.get("delete_grace_hours", 24)))
    confirmations = int(config.get("delete_min_confirmations", 2))
    return bool(
        config.get("delete_upstream_keys", True)
        and entry.get("upstream_key_id") is not None
        and not entry.get("upstream_key_deleted_at")
        and projected_count >= confirmations
        and now - missing_since >= grace
    )


def _assert_delete_ownership(
    snapshot: ProviderSnapshot,
    entry: dict[str, Any],
) -> UpstreamKey | None:
    key_id = str(entry["upstream_key_id"])
    key = snapshot.key_by_id(key_id)
    if key is None:
        return None
    marker = str(entry.get("marker") or "")
    if not marker or key.name != marker:
        raise ReconcileError(
            "ownership_conflict",
            f"refusing to delete upstream key {key_id} because its ownership marker changed",
        )
    resource_id = str(entry.get("resource_id") or "")
    expected_group = resource_id.removeprefix("group:")
    if expected_group and key.group_ref != expected_group:
        raise ReconcileError(
            "ownership_conflict",
            f"refusing to delete upstream key {key_id} because its group changed",
        )
    return key


def _action_plan(
    inventory: Inventory,
    state: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> list[Action]:
    config = config or {}
    actions: list[Action] = []
    now = datetime.now(UTC)
    current_ids: set[str] = set()
    state_resources = state.get("resources") if isinstance(state.get("resources"), dict) else {}
    for binding in inventory.bindings:
        resource = binding.resource
        state_id = resource_state_id(resource.provider_id, resource.resource_id)
        current_ids.add(state_id)
        key = resource.key
        if key is None:
            actions.append(Action("create_upstream_key", resource.provider_id, resource.resource_id, {"marker": resource.marker, "group_ref": resource.group_ref}))
        else:
            if key.name != resource.marker:
                actions.append(Action("rename_upstream_key", resource.provider_id, resource.resource_id, {"key_id": key.id, "from": key.name, "to": resource.marker}))
            if key.status not in ("active", "1", "enabled"):
                actions.append(Action("enable_upstream_key", resource.provider_id, resource.resource_id, {"key_id": key.id}))
        account = binding.target_account
        if account is None:
            detail = {"priority": resource.priority, "marker": resource.marker}
            if config.get("providers"):
                detail["concurrency"] = _target_concurrency(
                    config, resource.provider_id
                )
            actions.append(
                Action(
                    "create_target_account",
                    resource.provider_id,
                    resource.resource_id,
                    detail,
                )
            )
            continue
        account_id = int(account["id"])
        metadata = metadata_from_account(account)
        if key and metadata != desired_metadata(resource):
            actions.append(Action("update_target_metadata", resource.provider_id, resource.resource_id, {"account_id": account_id, "adopted": binding.adopted}))
        desired_base_url = str(
            inventory.providers[resource.provider_id].config["inference_base"]
        )
        current_base_url = str((account.get("credentials") or {}).get("base_url") or "")
        if key and (
            not metadata
            or metadata.get("key_fingerprint") != key.fingerprint
            or current_base_url.rstrip("/") != desired_base_url.rstrip("/")
        ):
            actions.append(
                Action(
                    "rotate_target_credential",
                    resource.provider_id,
                    resource.resource_id,
                    {
                        "account_id": account_id,
                        "key_id": key.id,
                        "base_url_changed": current_base_url.rstrip("/")
                        != desired_base_url.rstrip("/"),
                    },
                )
            )
        if int(account.get("priority") or 0) != int(resource.priority or 0):
            actions.append(Action("update_target_priority", resource.provider_id, resource.resource_id, {"account_id": account_id, "from": account.get("priority"), "to": resource.priority}))
        if config.get("providers"):
            desired_concurrency = _target_concurrency(config, resource.provider_id)
            if int(account.get("concurrency") or 0) != desired_concurrency:
                actions.append(
                    Action(
                        "update_target_concurrency",
                        resource.provider_id,
                        resource.resource_id,
                        {
                            "account_id": account_id,
                            "from": account.get("concurrency"),
                            "to": desired_concurrency,
                        },
                    )
                )
        if account.get("schedulable") is False:
            actions.append(Action("enable_target_account", resource.provider_id, resource.resource_id, {"account_id": account_id}))

    accounts_by_id = {
        int(item["id"]): item
        for item in inventory.target_accounts
        if item.get("id") is not None
    }
    for skipped in inventory.skipped_resources:
        provider_id = str(skipped.get("provider_id") or "")
        resource_id = str(skipped.get("resource_id") or "")
        state_id = resource_state_id(provider_id, resource_id)
        current_ids.add(state_id)
        entry = state_resources.get(state_id)
        account_id = (
            entry.get("target_account_id") if isinstance(entry, dict) else None
        )
        actions.append(
            Action(
                "defer_probe_resource",
                provider_id,
                resource_id,
                {
                    "reason": skipped.get("reason"),
                    "account_id": account_id,
                },
            )
        )
        account = (
            accounts_by_id.get(int(account_id)) if account_id is not None else None
        )
        if account is not None and account.get("schedulable") is not False:
            actions.append(
                Action(
                    "disable_target_account_probe_deferred",
                    provider_id,
                    resource_id,
                    {"account_id": int(account_id)},
                )
            )

    for state_id, entry in state_resources.items():
        if state_id in current_ids or not isinstance(entry, dict):
            continue
        provider_id, _, resource_id = state_id.partition("/")
        projected_count = int(entry.get("missing_count") or 0) + 1
        detail = {
            "account_id": entry.get("target_account_id"),
            "upstream_key_id": entry.get("upstream_key_id"),
            "missing_count": projected_count,
        }
        actions.append(Action("quarantine_missing_resource", provider_id, resource_id, detail))
        if _missing_delete_eligible(
            config, entry, projected_count=projected_count, now=now
        ):
            snapshot = inventory.snapshots.get(provider_id)
            if snapshot is None:
                raise ReconcileError(
                    "incomplete_scan", f"cannot verify deletion inventory for {provider_id}"
                )
            key = _assert_delete_ownership(snapshot, entry)
            actions.append(
                Action(
                    "delete_upstream_key" if key else "confirm_upstream_key_absent",
                    provider_id,
                    resource_id,
                    {
                        "upstream_key_id": entry.get("upstream_key_id"),
                        "missing_count": projected_count,
                        "missing_since": entry.get("missing_since"),
                    },
                )
            )
    return actions


def _observed_hash(inventory: Inventory) -> str:
    payload = {
        "resources": [binding.resource.safe_dict() for binding in inventory.bindings],
        "skipped_resources": inventory.skipped_resources,
        "targets": [
            {
                "id": binding.target_account_id,
                "priority": binding.target_account.get("priority") if binding.target_account else None,
                "concurrency": binding.target_account.get("concurrency") if binding.target_account else None,
                "schedulable": binding.target_account.get("schedulable") if binding.target_account else None,
                "metadata": metadata_from_account(binding.target_account),
            }
            for binding in inventory.bindings
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_plan(config: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], Inventory]:
    inventory = build_inventory(config, state)
    actions = _action_plan(inventory, state, config)
    plan = {
        "schema": 1,
        "observed_hash": _observed_hash(inventory),
        "resources": [binding.resource.safe_dict() | {"target_account_id": binding.target_account_id} for binding in inventory.bindings],
        "skipped_resources": inventory.skipped_resources,
        "actions": [action.safe_dict() for action in actions],
        "summary": {
            "providers": len(inventory.providers),
            "resources": len(inventory.bindings),
            "subscriptions": sum(1 for item in inventory.bindings if item.resource.source_class == "subscription"),
            "metered": sum(1 for item in inventory.bindings if item.resource.source_class == "metered"),
            "probe_deferred": len(inventory.skipped_resources),
            "actions": len(actions),
        },
    }
    return plan, inventory


def _snapshot_payload(inventory: Inventory, state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    managed_ids = {
        binding.target_account_id
        for binding in inventory.bindings
        if binding.target_account_id is not None
    }
    state_resources = state.get("resources") if isinstance(state.get("resources"), dict) else {}
    managed_ids.update(
        int(item["target_account_id"])
        for item in state_resources.values()
        if isinstance(item, dict) and item.get("target_account_id") is not None
    )
    return {
        "schema": 1,
        "created_at": utc_now(),
        "observed_hash": plan["observed_hash"],
        "state": state,
        "accounts": [
            {
                "id": int(item["id"]),
                "name": item.get("name"),
                "priority": item.get("priority"),
                "concurrency": item.get("concurrency"),
                "schedulable": item.get("schedulable"),
                "metadata": metadata_from_account(item),
            }
            for item in inventory.target_accounts
            if item.get("id") is not None and int(item["id"]) in managed_ids
        ],
    }


def _restore_target_routing(target: TargetSub2API, snapshot: dict[str, Any]) -> list[int]:
    failed: list[int] = []
    for account in snapshot.get("accounts", []):
        try:
            account_id = int(account["id"])
            if account.get("priority") is not None:
                target.bulk_update([account_id], priority=int(account["priority"]))
            if account.get("concurrency") is not None:
                target.bulk_update([account_id], concurrency=int(account["concurrency"]))
            if account.get("schedulable") is not None:
                target.set_schedulable(account_id, bool(account["schedulable"]))
        except Exception:
            failed.append(int(account.get("id") or 0))
    return failed


def _quarantine_probe_deferred_accounts(
    target: TargetSub2API, state: dict[str, Any]
) -> list[int]:
    resources = state.get("resources")
    if not isinstance(resources, dict):
        return [-1]
    failed: list[int] = []
    seen: set[int] = set()
    for entry in resources.values():
        if not isinstance(entry, dict) or entry.get("status") != "probe_deferred":
            continue
        account_id = entry.get("target_account_id")
        if account_id is None:
            continue
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            failed.append(-1)
            continue
        if normalized_id in seen:
            continue
        seen.add(normalized_id)
        try:
            target.set_schedulable(normalized_id, False)
        except Exception:
            failed.append(normalized_id)
    return failed


def _quarantine_new_managed_accounts(
    target: TargetSub2API, snapshot: dict[str, Any]
) -> list[int]:
    previous_ids = {int(item["id"]) for item in snapshot.get("accounts", []) if item.get("id") is not None}
    failed: list[int] = []
    try:
        current = target.list_accounts()
    except Exception:
        return [-1]
    for account in current:
        if account.get("id") is None or int(account["id"]) in previous_ids:
            continue
        if not metadata_from_account(account):
            continue
        try:
            target.set_schedulable(int(account["id"]), False)
        except Exception:
            failed.append(int(account["id"]))
    return failed


def _keychain_resource_account(resource: UpstreamResource) -> str:
    return f"resource:{resource.marker}:api_key"


def _target_account_name(resource: UpstreamResource) -> str:
    suffix = resource.marker.rsplit(":", 1)[-1]
    return f"bbta-{resource.provider_id}-{suffix}"


def _write_resource_state(
    state_resources: dict[str, Any], binding: Binding, account_id: int
) -> None:
    resource = binding.resource
    if not resource.key:
        raise ReconcileError("missing_upstream_key", f"cannot persist {resource.marker} without a key")
    state_resources[resource_state_id(resource.provider_id, resource.resource_id)] = {
        "provider_id": resource.provider_id,
        "resource_id": resource.resource_id,
        "marker": resource.marker,
        "upstream_key_id": resource.key.id,
        "target_account_id": account_id,
        "key_fingerprint": resource.key.fingerprint,
        "source_class": resource.source_class,
        "multiplier": decimal_text(resource.multiplier),
        "priority": resource.priority,
        "missing_since": None,
        "missing_count": 0,
        "status": "active",
    }


def _apply_active_resources(
    config: dict[str, Any],
    inventory: Inventory,
    state: dict[str, Any],
    *,
    record_mutation: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    record = record_mutation or (lambda _event: None)
    target_group_id = int(config["target"]["group_id"])
    state_resources = state.setdefault("resources", {})
    if not isinstance(state_resources, dict):
        raise ReconcileError("invalid_local_state", "state.resources is invalid")
    candidate_records = state.setdefault("candidate_probes", {})
    if not isinstance(candidate_records, dict):
        raise ReconcileError("invalid_local_state", "state.candidate_probes is invalid")

    # Phase 1: establish stable upstream ownership and secret custody.
    for binding in inventory.bindings:
        resource = binding.resource
        provider = inventory.providers[resource.provider_id]
        if resource.key is None:
            record({"phase": "intent", "kind": "create_upstream_key", "marker": resource.marker})
            resource.key = provider.create_key(resource)
            record(
                {
                    "phase": "done",
                    "kind": "create_upstream_key",
                    "marker": resource.marker,
                    "upstream_key_id": resource.key.id,
                }
            )
        if resource.key.name != resource.marker:
            record(
                {
                    "phase": "intent",
                    "kind": "rename_upstream_key",
                    "marker": resource.marker,
                    "upstream_key_id": resource.key.id,
                }
            )
            provider.rename_key(resource.key, resource.marker)
            resource.key.name = resource.marker
            record(
                {
                    "phase": "done",
                    "kind": "rename_upstream_key",
                    "marker": resource.marker,
                    "upstream_key_id": resource.key.id,
                }
            )
        if resource.key.status not in ("active", "1", "enabled"):
            record(
                {
                    "phase": "intent",
                    "kind": "enable_upstream_key",
                    "marker": resource.marker,
                    "upstream_key_id": resource.key.id,
                }
            )
            provider.enable_key(resource.key)
            resource.key.status = "active"
            record(
                {
                    "phase": "done",
                    "kind": "enable_upstream_key",
                    "marker": resource.marker,
                    "upstream_key_id": resource.key.id,
                }
            )
        keychain_set(_keychain_resource_account(resource), resource.key.secret)

    # Phase 2: establish/repair target ownership and credentials without exposing
    # a newly-created row to traffic before it has the intended group binding.
    for binding in inventory.bindings:
        resource = binding.resource
        assert resource.key is not None
        metadata = desired_metadata(resource)
        extra_patch = {METADATA_KEY: metadata}
        account = binding.target_account
        provider_config = next(
            item for item in config["providers"] if item["id"] == resource.provider_id
        )
        desired_concurrency = _target_concurrency(config, resource.provider_id)
        if account is None:
            record({"phase": "intent", "kind": "create_target_account", "marker": resource.marker})
            account = inventory.target.create_account(
                name=_target_account_name(resource),
                base_url=str(provider_config["inference_base"]),
                api_key=resource.key.secret,
                concurrency=desired_concurrency,
                priority=int(resource.priority or 1),
                extra=extra_patch,
            )
            inventory.target.set_schedulable(int(account["id"]), False)
            account["schedulable"] = False
            inventory.target.bulk_update([int(account["id"])], group_ids=[target_group_id])
            binding.target_account = account
            record(
                {
                    "phase": "done",
                    "kind": "create_target_account",
                    "marker": resource.marker,
                    "target_account_id": int(account["id"]),
                }
            )
        else:
            old_metadata = metadata_from_account(account)
            provider_config = next(
                item for item in config["providers"] if item["id"] == resource.provider_id
            )
            current_base_url = str((account.get("credentials") or {}).get("base_url") or "")
            desired_base_url = str(provider_config["inference_base"])
            if not old_metadata or (
                old_metadata.get("key_fingerprint") != resource.key.fingerprint
                or current_base_url.rstrip("/") != desired_base_url.rstrip("/")
            ):
                record(
                    {
                        "phase": "intent",
                        "kind": "rotate_target_credential",
                        "marker": resource.marker,
                        "target_account_id": int(account["id"]),
                    }
                )
                inventory.target.bulk_update(
                    [int(account["id"])],
                    credentials={
                        "base_url": desired_base_url,
                        "api_key": resource.key.secret,
                    },
                )
                record(
                    {
                        "phase": "done",
                        "kind": "rotate_target_credential",
                        "marker": resource.marker,
                        "target_account_id": int(account["id"]),
                    }
                )
            if old_metadata != metadata:
                record(
                    {
                        "phase": "intent",
                        "kind": "update_target_metadata",
                        "marker": resource.marker,
                        "target_account_id": int(account["id"]),
                    }
                )
                inventory.target.bulk_update([int(account["id"])], extra=extra_patch)
                record(
                    {
                        "phase": "done",
                        "kind": "update_target_metadata",
                        "marker": resource.marker,
                        "target_account_id": int(account["id"]),
                    }
                )
            if int(account.get("concurrency") or 0) != desired_concurrency:
                record(
                    {
                        "phase": "intent",
                        "kind": "update_target_concurrency",
                        "marker": resource.marker,
                        "target_account_id": int(account["id"]),
                        "concurrency": desired_concurrency,
                    }
                )
                inventory.target.bulk_update(
                    [int(account["id"])], concurrency=desired_concurrency
                )
                record(
                    {
                        "phase": "done",
                        "kind": "update_target_concurrency",
                        "marker": resource.marker,
                        "target_account_id": int(account["id"]),
                        "concurrency": desired_concurrency,
                    }
                )

    # Phase 3: dense global tiers. Grouping identical priorities in one target
    # call is the mechanical guarantee that equal multipliers share a tier.
    by_priority: dict[int, list[int]] = {}
    for binding in inventory.bindings:
        assert binding.target_account is not None
        if int(binding.target_account.get("priority") or 0) != int(
            binding.resource.priority or 1
        ):
            by_priority.setdefault(int(binding.resource.priority or 1), []).append(
                int(binding.target_account["id"])
            )
    for priority in sorted(by_priority, reverse=True):
        record(
            {
                "phase": "intent",
                "kind": "update_target_priority",
                "priority": priority,
                "target_account_ids": by_priority[priority],
            }
        )
        inventory.target.bulk_update(by_priority[priority], priority=priority)
        record(
            {
                "phase": "done",
                "kind": "update_target_priority",
                "priority": priority,
                "target_account_ids": by_priority[priority],
            }
        )

    for binding in inventory.bindings:
        assert binding.target_account is not None
        account_id = int(binding.target_account["id"])
        if binding.target_account.get("schedulable") is not True:
            record(
                {
                    "phase": "intent",
                    "kind": "set_target_schedulable",
                    "target_account_id": account_id,
                    "schedulable": True,
                }
            )
            inventory.target.set_schedulable(account_id, True)
            record(
                {
                    "phase": "done",
                    "kind": "set_target_schedulable",
                    "target_account_id": account_id,
                    "schedulable": True,
                }
            )
        _write_resource_state(state_resources, binding, account_id)
        candidate_records.pop(
            resource_state_id(
                binding.resource.provider_id, binding.resource.resource_id
            ),
            None,
        )


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _apply_probe_deferred_resources(
    inventory: Inventory,
    state: dict[str, Any],
    *,
    record_mutation: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    record = record_mutation or (lambda _event: None)
    state_resources = state.setdefault("resources", {})
    if not isinstance(state_resources, dict):
        raise ReconcileError("invalid_local_state", "state.resources is invalid")
    accounts_by_id = {
        int(item["id"]): item
        for item in inventory.target_accounts
        if item.get("id") is not None
    }
    for skipped in inventory.skipped_resources:
        provider_id = str(skipped.get("provider_id") or "")
        resource_id = str(skipped.get("resource_id") or "")
        state_id = resource_state_id(provider_id, resource_id)
        entry = state_resources.get(state_id)
        if not isinstance(entry, dict):
            continue
        entry["status"] = "probe_deferred"
        entry["missing_since"] = None
        entry["missing_count"] = 0
        account_id = entry.get("target_account_id")
        account = (
            accounts_by_id.get(int(account_id)) if account_id is not None else None
        )
        if account is None or account.get("schedulable") is False:
            continue
        record(
            {
                "phase": "intent",
                "kind": "set_target_schedulable",
                "provider_id": provider_id,
                "resource_id": resource_id,
                "target_account_id": int(account_id),
                "schedulable": False,
                "reason": "probe_deferred",
            }
        )
        inventory.target.set_schedulable(int(account_id), False)
        account["schedulable"] = False
        record(
            {
                "phase": "done",
                "kind": "set_target_schedulable",
                "provider_id": provider_id,
                "resource_id": resource_id,
                "target_account_id": int(account_id),
                "schedulable": False,
                "reason": "probe_deferred",
            }
        )


def _apply_missing_resources(
    config: dict[str, Any],
    inventory: Inventory,
    state: dict[str, Any],
    *,
    increment: bool = True,
    allow_delete: bool = True,
    planned_deletions: dict[str, str] | None = None,
    record_mutation: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    record = record_mutation or (lambda _event: None)
    current = {
        resource_state_id(binding.resource.provider_id, binding.resource.resource_id)
        for binding in inventory.bindings
    }
    current.update(
        resource_state_id(
            str(item.get("provider_id") or ""),
            str(item.get("resource_id") or ""),
        )
        for item in inventory.skipped_resources
    )
    state_resources = state.setdefault("resources", {})
    candidate_records = state.setdefault("candidate_probes", {})
    if not isinstance(candidate_records, dict):
        raise ReconcileError("invalid_local_state", "state.candidate_probes is invalid")
    now = datetime.now(UTC)
    accounts_by_id = {
        int(item["id"]): item
        for item in inventory.target_accounts
        if item.get("id") is not None
    }
    fresh_delete_snapshots: dict[str, ProviderSnapshot] = {}
    for state_id, entry in list(state_resources.items()):
        if state_id in current or not isinstance(entry, dict):
            continue
        account_id = entry.get("target_account_id")
        account = accounts_by_id.get(int(account_id)) if account_id is not None else None
        if account_id is not None and (not account or account.get("schedulable") is not False):
            record(
                {
                    "phase": "intent",
                    "kind": "set_target_schedulable",
                    "target_account_id": int(account_id),
                    "schedulable": False,
                }
            )
            inventory.target.set_schedulable(int(account_id), False)
            if account:
                account["schedulable"] = False
            record(
                {
                    "phase": "done",
                    "kind": "set_target_schedulable",
                    "target_account_id": int(account_id),
                    "schedulable": False,
                }
            )
        missing_since = _parse_when(entry.get("missing_since"))
        if missing_since is None:
            missing_since = now
            entry["missing_since"] = missing_since.isoformat()
        if increment:
            entry["missing_count"] = int(entry.get("missing_count") or 0) + 1
        entry["status"] = "quarantined"
        provider_id = str(entry.get("provider_id") or state_id.partition("/")[0])
        provider = inventory.providers.get(provider_id)
        key_id = entry.get("upstream_key_id")
        if (
            allow_delete
            and planned_deletions is not None
            and state_id in planned_deletions
            and provider
            and key_id is not None
            and _missing_delete_eligible(
                config,
                entry,
                projected_count=int(entry.get("missing_count") or 0),
                now=now,
            )
        ):
            fresh_snapshot = fresh_delete_snapshots.get(provider_id)
            if fresh_snapshot is None:
                fresh_snapshot = provider.scan()
                fresh_delete_snapshots[provider_id] = fresh_snapshot
            resource_id = str(entry.get("resource_id") or state_id.partition("/")[2])
            if any(item.resource_id == resource_id for item in fresh_snapshot.resources):
                raise ReconcileError(
                    "inventory_changed",
                    f"refusing to delete {provider_id}/{resource_id} because it became active again",
                )
            current_key = _assert_delete_ownership(fresh_snapshot, entry)
            planned_kind = planned_deletions[state_id]
            if planned_kind == "confirm_upstream_key_absent" and current_key is not None:
                raise ReconcileError(
                    "inventory_changed",
                    f"refusing to delete {provider_id}/{resource_id} because its key reappeared after planning",
                )
            if current_key is None:
                entry["upstream_key_deleted_at"] = now.isoformat()
                entry["status"] = "upstream_key_deleted_target_retained"
                candidate_records.pop(state_id, None)
                record(
                    {
                        "phase": "done",
                        "kind": "confirm_upstream_key_absent",
                        "provider_id": provider_id,
                        "upstream_key_id": str(key_id),
                    }
                )
                continue
            record(
                {
                    "phase": "intent",
                    "kind": "delete_upstream_key",
                    "provider_id": provider_id,
                    "upstream_key_id": str(key_id),
                }
            )
            provider.delete_key(str(key_id))
            entry["upstream_key_deleted_at"] = now.isoformat()
            entry["status"] = "upstream_key_deleted_target_retained"
            candidate_records.pop(state_id, None)
            record(
                {
                    "phase": "done",
                    "kind": "delete_upstream_key",
                    "provider_id": provider_id,
                    "upstream_key_id": str(key_id),
                }
            )


def _verify_active(config: dict[str, Any], state: dict[str, Any]) -> None:
    inventory = build_inventory(config, state)
    for binding in inventory.bindings:
        resource = binding.resource
        account = binding.target_account
        if not resource.key or resource.key.name != resource.marker:
            raise ReconcileError("verification_failed", f"upstream ownership verification failed for {resource.marker}")
        if not account:
            raise ReconcileError("verification_failed", f"target account is missing for {resource.marker}")
        if int(account.get("priority") or 0) != int(resource.priority or 0):
            raise ReconcileError("verification_failed", f"priority verification failed for {resource.marker}")
        if int(account.get("concurrency") or 0) != _target_concurrency(
            config, resource.provider_id
        ):
            raise ReconcileError(
                "verification_failed", f"concurrency verification failed for {resource.marker}"
            )
        if account.get("schedulable") is not True:
            raise ReconcileError("verification_failed", f"schedulable verification failed for {resource.marker}")
        if metadata_from_account(account) != desired_metadata(resource):
            raise ReconcileError("verification_failed", f"metadata verification failed for {resource.marker}")
        desired_base_url = str(
            next(
                item["inference_base"]
                for item in config["providers"]
                if item["id"] == resource.provider_id
            )
        )
        current_base_url = str((account.get("credentials") or {}).get("base_url") or "")
        if current_base_url.rstrip("/") != desired_base_url.rstrip("/"):
            raise ReconcileError(
                "verification_failed", f"base URL verification failed for {resource.marker}"
            )


def _prequalification_context(
    config: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], Inventory]:
    target = TargetSub2API(config["target"])
    settings = _validate_target(config, target)
    target_accounts = target.list_accounts()
    state_digest = hashlib.sha256(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    plan = {
        "schema": 1,
        "phase": "candidate_qualification",
        "observed_hash": state_digest,
        "resources": [],
        "skipped_resources": [],
        "actions": [],
        "summary": {
            "providers": len(config["providers"]),
            "resources": 0,
            "subscriptions": 0,
            "metered": 0,
            "probe_deferred": 0,
            "actions": 0,
        },
    }
    inventory = Inventory(
        providers={},
        snapshots={},
        target=target,
        target_accounts=target_accounts,
        bindings=[],
        settings=settings,
        skipped_resources=[],
    )
    return plan, inventory


def reconcile_plan(config_path: Path | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    state_dir = default_state_dir()
    state = load_json(state_dir / "state.json", {"schema": 1, "resources": {}})
    if not isinstance(state, dict):
        raise ReconcileError("invalid_local_state", "state root is invalid")
    plan, _ = build_plan(config, state)
    return plan


def _assert_maintenance_safe_plan(
    plan: dict[str, Any],
    *,
    min_active_resources: int,
) -> None:
    actions = plan.get("actions")
    resources = plan.get("resources")
    if not isinstance(actions, list) or not isinstance(resources, list):
        raise ReconcileError(
            "maintenance_plan_unsafe",
            "maintenance apply produced an invalid plan",
        )
    kinds = {
        str(item.get("kind"))
        for item in actions
        if isinstance(item, dict) and item.get("kind")
    }
    if (
        "delete_upstream_key" in kinds
        or "confirm_upstream_key_absent" in kinds
        or any(kind.startswith("quarantine") for kind in kinds)
        or len(resources) < min_active_resources
    ):
        raise ReconcileError(
            "maintenance_plan_unsafe",
            "maintenance apply plan violates the non-destructive resource floor",
            context={
                "active_resource_floor": min_active_resources,
                "planned_resources": len(resources),
            },
        )


def reconcile_apply(
    config_path: Path | None = None,
    *,
    maintenance_safe: bool = False,
    min_active_resources: int | None = None,
) -> dict[str, Any]:
    if maintenance_safe:
        if (
            isinstance(min_active_resources, bool)
            or not isinstance(min_active_resources, int)
            or min_active_resources < 0
        ):
            raise ReconcileError(
                "invalid_maintenance_guard",
                "maintenance-safe apply requires a non-negative active resource floor",
            )
    elif min_active_resources is not None:
        raise ReconcileError(
            "invalid_maintenance_guard",
            "active resource floor requires maintenance-safe apply",
        )
    config = load_config(config_path)
    state_dir = default_state_dir()
    state_path = state_dir / "state.json"
    audit_path = state_dir / "audit.jsonl"
    pending_path = state_dir / "pending-run.json"
    snapshots_dir = state_dir / "snapshots"
    with exclusive_lock(state_dir):
        state = load_json(state_path, {"schema": 1, "resources": {}})
        if not isinstance(state, dict):
            raise ReconcileError("invalid_local_state", "state root is invalid")
        plan, inventory = _prequalification_context(config, state)
        probe_results: list[dict[str, Any]] = []
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        snapshot_path = snapshots_dir / f"{run_id}.json"
        snapshot = _snapshot_payload(inventory, state, plan)
        atomic_write_json(snapshot_path, snapshot)
        previous_pending = load_json(pending_path, None)
        pending: dict[str, Any] = {
            "schema": 1,
            "run_id": run_id,
            "started_at": utc_now(),
            "plan": plan,
            "mutations": [],
        }
        if isinstance(previous_pending, dict) and previous_pending.get("run_id"):
            pending["recovery_of_run_id"] = previous_pending["run_id"]
            recovery_chain = previous_pending.get("recovery_chain")
            pending["recovery_chain"] = (
                list(recovery_chain) if isinstance(recovery_chain, list) else []
            )[-9:] + [
                {
                    "run_id": previous_pending["run_id"],
                    "started_at": previous_pending.get("started_at"),
                    "mutations": previous_pending.get("mutations", []),
                }
            ]
        atomic_write_json(pending_path, pending)

        def record_mutation(event: dict[str, Any]) -> None:
            pending["mutations"].append({"at": utc_now(), **event})
            atomic_write_json(pending_path, pending)

        append_audit(audit_path, {"event": "run_started", "run_id": run_id, "plan": plan})
        try:
            recovered_probe_keys = _recover_pending_probe_keys(
                config,
                state,
                previous_pending if isinstance(previous_pending, dict) else None,
                audit_path=audit_path,
                persist_state=lambda current_state: atomic_write_json(
                    state_path, current_state
                ),
            )
            if recovered_probe_keys:
                pending["recovered_probe_keys"] = recovered_probe_keys
                atomic_write_json(pending_path, pending)
            probe_results = _qualify_pending_resources(
                config,
                state,
                audit_path=audit_path,
                record_mutation=record_mutation,
                persist_state=lambda current_state: atomic_write_json(
                    state_path, current_state
                ),
            )
            # Candidate state is a durable observation and recovery handle. It is
            # intentionally persisted before target routing starts.
            atomic_write_json(state_path, state)
            plan, inventory = build_plan(config, state)
            if maintenance_safe:
                assert min_active_resources is not None
                _assert_maintenance_safe_plan(
                    plan,
                    min_active_resources=min_active_resources,
                )
            # Prequalification intentionally has no bindings. Refresh the target
            # routing snapshot after the full locked inventory is built, but
            # before any target account mutation, so first-time adoptions are
            # also restorable.
            snapshot = _snapshot_payload(inventory, state, plan)
            atomic_write_json(snapshot_path, snapshot)
            pending["plan"] = plan
            pending["qualification"] = {
                "attempted": len(probe_results),
                "compatible": sum(
                    1 for item in probe_results if item.get("compatible") is True
                ),
                "deferred": sum(
                    1 for item in probe_results if item.get("compatible") is not True
                ),
            }
            atomic_write_json(pending_path, pending)
            append_audit(
                audit_path,
                {
                    "event": "candidate_qualification_completed",
                    "run_id": run_id,
                    **pending["qualification"],
                },
            )
            planned_deletions = {
                resource_state_id(str(item["provider_id"]), str(item["resource_id"])): str(
                    item["kind"]
                )
                for item in plan["actions"]
                if item["kind"] in ("delete_upstream_key", "confirm_upstream_key_absent")
            }
            _apply_probe_deferred_resources(
                inventory,
                state,
                record_mutation=record_mutation,
            )
            # Remove confirmed-inactive resources from scheduling before any tier
            # compaction can temporarily place a more expensive route alongside them.
            _apply_missing_resources(
                config,
                inventory,
                state,
                increment=True,
                allow_delete=False,
                record_mutation=record_mutation,
            )
            _apply_active_resources(
                config, inventory, state, record_mutation=record_mutation
            )
            _verify_active(config, state)
            # Irreversible upstream deletion is last, after active read-back passed.
            _apply_missing_resources(
                config,
                inventory,
                state,
                increment=False,
                allow_delete=True,
                planned_deletions=planned_deletions,
                record_mutation=record_mutation,
            )
            state["schema"] = 1
            state["last_run"] = {
                "id": run_id,
                "status": "ok",
                "completed_at": utc_now(),
                "observed_hash": plan["observed_hash"],
                "actions": len(plan["actions"]),
                "probes": {
                    "attempted": len(probe_results),
                    "compatible": sum(
                        1 for item in probe_results if item.get("compatible") is True
                    ),
                    "deferred": sum(
                        1 for item in probe_results if item.get("compatible") is not True
                    ),
                },
                "snapshot": str(snapshot_path),
            }
            atomic_write_json(state_path, state)
            append_audit(
                audit_path,
                {
                    "event": "run_completed",
                    "run_id": run_id,
                    "actions": len(plan["actions"]),
                    "snapshot": str(snapshot_path),
                },
            )
            pending_path.unlink(missing_ok=True)
        except Exception as exc:
            cause_code = str(getattr(exc, "code", "unexpected_error"))
            cause_context = getattr(exc, "context", {})
            if not isinstance(cause_context, dict):
                cause_context = {}
            failed_restore = _quarantine_new_managed_accounts(inventory.target, snapshot)
            failed_restore.extend(_restore_target_routing(inventory.target, snapshot))
            failed_restore.extend(
                _quarantine_probe_deferred_accounts(inventory.target, state)
            )
            if (
                maintenance_safe
                and cause_code == "maintenance_plan_unsafe"
                and not pending["mutations"]
                and not failed_restore
            ):
                pending_path.unlink(missing_ok=True)
            append_audit(
                audit_path,
                {
                    "event": "run_failed",
                    "run_id": run_id,
                    "error_type": type(exc).__name__,
                    "error_code": cause_code,
                    "routing_restore_failed_ids": failed_restore,
                    "pending_recovery": (
                        str(pending_path) if pending_path.exists() else None
                    ),
                    "mutation_count": len(pending["mutations"]),
                    "snapshot": str(snapshot_path),
                },
            )
            if failed_restore:
                raise ReconcileError(
                    "degraded_rollback",
                    "reconciliation failed and one or more target routing rows could not be restored",
                    next_action=f"inspect snapshot {snapshot_path}",
                    context={"cause_code": cause_code, **cause_context},
                ) from exc
            if pending["mutations"]:
                raise ReconcileError(
                    "partial_external_mutation",
                    "reconciliation stopped after one or more durable mutations; the next run will recover from managed markers",
                    next_action=f"inspect {pending_path}, then rerun apply",
                    context={"cause_code": cause_code, **cause_context},
                ) from exc
            raise
    return {
        "ok": True,
        "run_id": run_id,
        "actions": len(plan["actions"]),
        "resources": len(plan["resources"]),
        "probes": {
            "attempted": len(probe_results),
            "compatible": sum(
                1 for item in probe_results if item.get("compatible") is True
            ),
            "deferred": sum(
                1 for item in probe_results if item.get("compatible") is not True
            ),
            "results": probe_results,
        },
        "snapshot": str(snapshot_path),
    }


def reconcile_status() -> dict[str, Any]:
    state_dir = default_state_dir()
    state = load_json(state_dir / "state.json", {"schema": 1, "resources": {}})
    resources = state.get("resources") if isinstance(state, dict) else {}
    values = list(resources.values()) if isinstance(resources, dict) else []
    return {
        "configured": default_config_path().exists(),
        "last_run": state.get("last_run") if isinstance(state, dict) else None,
        "resources": len(values),
        "active": sum(1 for item in values if isinstance(item, dict) and item.get("status") == "active"),
        "quarantined": sum(1 for item in values if isinstance(item, dict) and item.get("status") != "active"),
        "state_dir": str(state_dir),
        "pending_recovery": (state_dir / "pending-run.json").exists(),
    }


def enroll_from_edge(
    config_path: Path | None = None, *, rotate_target_admin_key: bool = False
) -> dict[str, Any]:
    config = load_config(config_path)
    edge = config.get("edge") if isinstance(config.get("edge"), dict) else {}
    host = str(edge.get("host") or "127.0.0.1")
    port = int(edge.get("port") or 9222)
    enrolled: list[str] = []
    for provider in config["providers"]:
        if provider["type"] == "sub2api":
            enroll_sub2api_provider(provider, cdp_host=host, cdp_port=port)
        else:
            enroll_newapi_provider(provider, cdp_host=host, cdp_port=port)
        enrolled.append(str(provider["id"]))
    enroll_target(
        config["target"],
        cdp_host=host,
        cdp_port=port,
        rotate_existing=rotate_target_admin_key,
    )
    return {"ok": True, "providers": enrolled, "target": "enrolled"}


def enroll_provider_login(
    provider_id: str,
    account: str,
    password: str,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    provider_config = next(
        (item for item in config["providers"] if str(item["id"]) == provider_id), None
    )
    if provider_config is None:
        raise ReconcileError("invalid_config", f"unknown provider: {provider_id}")
    client = provider_from_config(provider_config)
    client.login_with_credentials(account, password)
    keychain_set(client.secret_account("login_account"), account)
    keychain_set(client.secret_account("login_password"), password)
    return {"ok": True, "provider": provider_id, "auth": "api-login-enrolled"}


def rollback_snapshot(snapshot_path: Path, config_path: Path | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    snapshot = load_json(snapshot_path, None)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("accounts"), list):
        raise ReconcileError("invalid_snapshot", "snapshot is invalid")
    target = TargetSub2API(config["target"])
    failed = _restore_target_routing(target, snapshot)
    if failed:
        raise ReconcileError("rollback_failed", f"routing rollback failed for {len(failed)} accounts")
    return {
        "ok": True,
        "restored_accounts": len(snapshot["accounts"]),
        "scope": "target priority and schedulable fields only",
    }
