from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Iterable, Literal


MANAGED_PREFIX = "bbta:v1"
PROBE_PREFIX = "bbta:probe:v1"
METADATA_KEY = "babata_upstream_reconciler"
METERED_PRIORITY_SCALE = Decimal("1000")
DEFAULT_SUBSCRIPTION_PRIORITY = 40


class ReconcileError(RuntimeError):
    """A fail-closed reconciliation error safe to show to the operator."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        next_action: str | None = None,
        context: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.next_action = next_action
        self.context = dict(context or {})


def decimal_value(value: Any, *, field_name: str = "multiplier") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReconcileError("invalid_multiplier", f"{field_name} is not a decimal") from exc
    if not result.is_finite() or result < 0:
        raise ReconcileError("invalid_multiplier", f"{field_name} must be finite and >= 0")
    return result.normalize()


def decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def marker_for(provider_id: str, resource_id: str) -> str:
    digest = hashlib.sha256(f"{provider_id}\0{resource_id}".encode("utf-8")).hexdigest()[:12]
    return f"{MANAGED_PREFIX}:{provider_id}:{digest}"


def probe_marker_for(provider_id: str, resource_id: str) -> str:
    digest = hashlib.sha256(f"probe\0{provider_id}\0{resource_id}".encode("utf-8")).hexdigest()[:12]
    return f"{PROBE_PREFIX}:{provider_id}:{digest}"


def fingerprint_secret(value: str) -> str:
    if not value:
        raise ReconcileError("missing_key_secret", "upstream key secret is missing")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class UpstreamKey:
    id: str
    name: str
    group_ref: str
    status: str
    secret: str = field(repr=False, compare=False)

    @property
    def fingerprint(self) -> str:
        return fingerprint_secret(self.secret)


@dataclass
class UpstreamResource:
    provider_id: str
    resource_id: str
    group_ref: str
    group_name: str
    source_class: Literal["subscription", "metered"]
    multiplier: Decimal | None
    key: UpstreamKey | None = None
    priority: int | None = None

    @property
    def marker(self) -> str:
        return marker_for(self.provider_id, self.resource_id)

    @property
    def probe_marker(self) -> str:
        return probe_marker_for(self.provider_id, self.resource_id)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "resource_id": self.resource_id,
            "group_ref": self.group_ref,
            "group_name": self.group_name,
            "source_class": self.source_class,
            "multiplier": decimal_text(self.multiplier),
            "priority": self.priority,
            "marker": self.marker,
            "upstream_key_id": self.key.id if self.key else None,
            "upstream_key_name": self.key.name if self.key else None,
            "key_fingerprint": self.key.fingerprint if self.key else None,
        }


@dataclass
class Binding:
    resource: UpstreamResource
    target_account: dict[str, Any] | None
    adopted: bool = False

    @property
    def target_account_id(self) -> int | None:
        if not self.target_account:
            return None
        return int(self.target_account["id"])


@dataclass(frozen=True)
class Action:
    kind: str
    provider_id: str
    resource_id: str
    detail: dict[str, Any]

    @property
    def id(self) -> str:
        stable = repr((self.kind, self.provider_id, self.resource_id, sorted(self.detail.items())))
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]

    def safe_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider_id": self.provider_id,
            "resource_id": self.resource_id,
            "detail": self.detail,
        }


def assign_priorities(
    resources: Iterable[UpstreamResource],
    *,
    subscription_priority: int = DEFAULT_SUBSCRIPTION_PRIORITY,
) -> list[UpstreamResource]:
    """Assign tiers to resources that have already passed qualification."""

    if isinstance(subscription_priority, bool) or subscription_priority < 2:
        raise ReconcileError(
            "invalid_subscription_priority",
            "subscription priority must be an integer >= 2",
        )
    threshold = Decimal(subscription_priority) / METERED_PRIORITY_SCALE
    items = list(resources)
    if not items:
        raise ReconcileError("empty_inventory", "no eligible upstream groups were found")

    for item in items:
        if item.source_class == "subscription":
            if item.multiplier is not None:
                # Subscription billing multiplier is intentionally ignored for routing.
                item.multiplier = None
            item.priority = subscription_priority
        elif item.source_class != "metered" or item.multiplier is None:
            raise ReconcileError(
                "unclassified_group",
                f"{item.provider_id}/{item.resource_id} has no authoritative class or multiplier",
            )
        else:
            raw_priority = int(
                (item.multiplier * METERED_PRIORITY_SCALE).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            if item.multiplier < threshold:
                item.priority = min(subscription_priority - 1, max(1, raw_priority))
            elif item.multiplier == threshold:
                item.priority = subscription_priority
            else:
                item.priority = max(subscription_priority + 1, raw_priority)

    return sorted(
        items,
        key=lambda item: (
            item.priority or 10**9,
            item.provider_id,
            item.resource_id,
        ),
    )


def desired_metadata(resource: UpstreamResource) -> dict[str, Any]:
    if not resource.key:
        raise ReconcileError("missing_upstream_key", f"{resource.marker} has no upstream key")
    return {
        "schema": 1,
        "managed_by": "sub2cli-upstream-reconciler",
        "provider_id": resource.provider_id,
        "resource_id": resource.resource_id,
        "upstream_key_id": resource.key.id,
        "key_fingerprint": resource.key.fingerprint,
        "source_class": resource.source_class,
        "multiplier": decimal_text(resource.multiplier),
        "marker": resource.marker,
    }


def metadata_from_account(account: dict[str, Any] | None) -> dict[str, Any] | None:
    if not account:
        return None
    extra = account.get("extra") or {}
    value = extra.get(METADATA_KEY)
    return value if isinstance(value, dict) else None


SENSITIVE_NAMES = (
    "api_key",
    "authorization",
    "cookie",
    "credentials",
    "password",
    "refresh_token",
    "secret",
    "session",
    "token",
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(name in lowered for name in SENSITIVE_NAMES):
                out[str(key)] = "<redacted>"
            else:
                out[str(key)] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str) and (value.startswith("sk-") or value.count(".") == 2 and len(value) > 80):
        return "<redacted>"
    return value
