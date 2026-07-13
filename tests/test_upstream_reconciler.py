from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from upstream_reconciler.clients import NewAPIProvider, ProviderSnapshot, Sub2APIProvider
from upstream_reconciler.core import (
    METADATA_KEY,
    Binding,
    ReconcileError,
    UpstreamKey,
    UpstreamResource,
    assign_priorities,
    decimal_text,
    desired_metadata,
    fingerprint_secret,
    marker_for,
    redact,
)
from upstream_reconciler.probe import (
    PROBE_POLICY,
    REQUIRED_MODEL,
    RESPONSE_CONTRACT,
    ProbeResult,
    probe_codex_responses,
    select_codex_model,
)
from upstream_reconciler.notify import notify_reconcile_error
from upstream_reconciler.runtime import (
    Inventory,
    _assert_maintenance_safe_plan,
    _action_plan,
    _apply_active_resources,
    _apply_missing_resources,
    _apply_probe_deferred_resources,
    _choose_key,
    _choose_target_account,
    _observed_hash,
    _probe_policy_hash,
    _qualify_pending_resources,
    _recover_pending_probe_keys,
    _restore_target_routing,
    _resource_probe_gate,
    _seed_inactive_adoptions,
    _snapshot_payload,
    _verify_active,
    reconcile_apply,
    validate_config,
)
from upstream_reconciler.store import atomic_write_json, keychain_get


def resource(
    provider: str,
    resource_id: str,
    source_class: str,
    multiplier: str | None,
) -> UpstreamResource:
    return UpstreamResource(
        provider_id=provider,
        resource_id=resource_id,
        group_ref=resource_id.removeprefix("group:"),
        group_name=resource_id,
        source_class=source_class,  # type: ignore[arg-type]
        multiplier=Decimal(multiplier) if multiplier is not None else None,
    )


class PriorityTests(unittest.TestCase):
    def test_subscription_then_dense_equal_multiplier_tiers(self) -> None:
        items = [
            resource("a", "group:sub", "subscription", None),
            resource("a", "group:5", "metered", "0.05"),
            resource("b", "group:5", "metered", "0.050"),
            resource("b", "group:8", "metered", "0.08"),
            resource("a", "group:15", "metered", "0.15"),
        ]
        result = assign_priorities(items)
        actual = {(item.provider_id, item.resource_id): item.priority for item in result}
        self.assertEqual(actual[("a", "group:sub")], 40)
        self.assertEqual(actual[("a", "group:5")], 50)
        self.assertEqual(actual[("b", "group:5")], 50)
        self.assertEqual(actual[("b", "group:8")], 80)
        self.assertEqual(actual[("a", "group:15")], 150)

    def test_provider_order_does_not_change_tiers(self) -> None:
        left = assign_priorities(
            [resource("z", "group:b", "metered", "0.3"), resource("a", "group:a", "metered", "0.1")]
        )
        right = assign_priorities(
            [resource("a", "group:a", "metered", "0.1"), resource("z", "group:b", "metered", "0.3")]
        )
        self.assertEqual(
            {(x.provider_id, x.resource_id): x.priority for x in left},
            {(x.provider_id, x.resource_id): x.priority for x in right},
        )

    def test_empty_inventory_fails_closed(self) -> None:
        with self.assertRaisesRegex(ReconcileError, "no eligible"):
            assign_priorities([])


class OwnershipTests(unittest.TestCase):
    def test_marker_is_stable_and_does_not_contain_group_name(self) -> None:
        marker = marker_for("code-plan", "group:特惠分组")
        self.assertEqual(marker, marker_for("code-plan", "group:特惠分组"))
        self.assertTrue(marker.startswith("bbta:v1:code-plan:"))
        self.assertNotIn("特惠", marker)

    def test_safe_resource_output_never_contains_secret(self) -> None:
        item = resource("a", "group:1", "metered", "0.05")
        item.priority = 2
        item.key = UpstreamKey("7", "managed", "1", "active", "sk-super-secret")
        rendered = json.dumps(item.safe_dict())
        self.assertNotIn("sk-super-secret", rendered)
        self.assertIn("sha256:", rendered)

    def test_nested_redaction(self) -> None:
        value = {
            "api_key": "sk-secret",
            "nested": {"refresh_token": "refresh", "safe": 1},
            "text": "sk-another-secret",
        }
        rendered = json.dumps(redact(value))
        self.assertNotIn("sk-secret", rendered)
        self.assertNotIn('"refresh"', rendered)
        self.assertIn('"safe": 1', rendered)

    def test_conflicting_key_sources_fail_closed(self) -> None:
        item = resource("a", "group:1", "metered", "0.05")
        snapshot = ProviderSnapshot(
            "a",
            [item],
            [
                UpstreamKey("1", item.marker, "1", "active", "sk-one"),
                UpstreamKey("2", "old", "1", "active", "sk-two"),
            ],
        )
        with self.assertRaisesRegex(ReconcileError, "conflicting"):
            _choose_key(item, snapshot, {"key_id": 2}, None)

    def test_adoption_selects_exact_target(self) -> None:
        item = resource("a", "group:1", "metered", "0.05")
        account = {"id": 9, "platform": "openai", "type": "apikey", "extra": {}}
        selected, adopted = _choose_target_account(
            item,
            {9: account},
            {},
            {"account_id": 9},
            None,
        )
        self.assertEqual(selected, account)
        self.assertTrue(adopted)


class ProviderNormalizationTests(unittest.TestCase):
    def test_sub2api_uses_authoritative_platform_and_active_subscription(self) -> None:
        config = {
            "id": "codex",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "adopt": [{"resource_id": "group:54", "key_id": 10, "account_id": 20}],
        }
        client = Sub2APIProvider(config)
        groups = [
            {"id": 1, "name": "sub", "status": "active", "platform": "openai", "subscription_type": "subscription", "rate_multiplier": 1},
            {"id": 2, "name": "openai", "status": "active", "platform": "openai", "subscription_type": "standard", "rate_multiplier": 0.15},
            {"id": 3, "name": "anthropic", "status": "active", "platform": "anthropic", "subscription_type": "standard", "rate_multiplier": 0.01},
        ]
        hidden_group = {"id": 54, "name": "hidden", "status": "active", "platform": "openai", "subscription_type": "standard", "rate_multiplier": 0.065}
        raw_keys = [{"id": 10, "name": "old", "group_id": 54, "group": hidden_group, "status": "active", "key": "sk-hidden"}]
        with mock.patch.object(client, "_request", side_effect=[groups, [{"group_id": 1, "status": "active"}], {}]), mock.patch.object(
            client, "_list_keys", return_value=(raw_keys, {"10": raw_keys[0]})
        ):
            snapshot = client.scan()
        by_id = {item.resource_id: item for item in snapshot.resources}
        self.assertEqual(set(by_id), {"group:1", "group:2", "group:54"})
        self.assertEqual(by_id["group:1"].source_class, "subscription")
        self.assertIsNone(by_id["group:1"].multiplier)
        self.assertEqual(by_id["group:54"].multiplier, Decimal("0.065"))

    def test_sub2api_user_rate_override_is_authoritative(self) -> None:
        config = {
            "id": "codex",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [{"id": 2, "name": "openai", "status": "active", "platform": "openai", "subscription_type": "standard", "rate_multiplier": 0.15}]
        with mock.patch.object(client, "_request", side_effect=[groups, [], {"2": 0.065}]), mock.patch.object(
            client, "_list_keys", return_value=([], {})
        ):
            snapshot = client.scan()
        self.assertEqual(snapshot.resources[0].multiplier, Decimal("0.065"))

    def test_sub2api_subscription_only_excludes_metered_groups(self) -> None:
        config = {
            "id": "subscription-site",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "subscription_only": True,
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [
            {
                "id": 1,
                "name": "subscription",
                "status": "active",
                "platform": "openai",
                "subscription_type": "subscription",
                "rate_multiplier": 1,
            },
            {
                "id": 2,
                "name": "metered",
                "status": "active",
                "platform": "openai",
                "subscription_type": "standard",
                "rate_multiplier": 0.22,
            },
        ]
        with mock.patch.object(
            client,
            "_request",
            side_effect=[groups, [{"group_id": 1, "status": "active"}], {}],
        ), mock.patch.object(client, "_list_keys", return_value=([], {})):
            snapshot = client.scan()
        self.assertEqual([item.resource_id for item in snapshot.resources], ["group:1"])
        self.assertEqual(snapshot.resources[0].source_class, "subscription")

    def test_sub2api_subscription_allowlist_rejects_unapproved_subscription(self) -> None:
        config = {
            "id": "subscription-site",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "subscription_resource_allowlist": ["group:1"],
            "require_subscription_expiry": True,
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [
            {
                "id": 1,
                "name": "approved",
                "status": "active",
                "platform": "openai",
                "subscription_type": "subscription",
                "rate_multiplier": 1,
            },
            {
                "id": 2,
                "name": "future-unapproved",
                "status": "active",
                "platform": "openai",
                "subscription_type": "subscription",
                "rate_multiplier": 1,
            },
        ]
        active = [
            {
                "group_id": 1,
                "status": "active",
                "starts_at": "2026-01-01T00:00:00Z",
                "expires_at": "2027-01-01T00:00:00Z",
            },
            {
                "group_id": 2,
                "status": "active",
                "starts_at": "2026-01-01T00:00:00Z",
                "expires_at": "not-a-time",
            },
        ]
        with mock.patch.object(
            client, "_request", side_effect=[groups, active, {}]
        ), mock.patch.object(client, "_list_keys", return_value=([], {})), mock.patch(
            "upstream_reconciler.clients._utc_now",
            return_value=datetime(2026, 7, 12, tzinfo=UTC),
        ):
            snapshot = client.scan()
        self.assertEqual([item.resource_id for item in snapshot.resources], ["group:1"])

    def test_sub2api_expired_allowlisted_subscription_is_not_managed(self) -> None:
        config = {
            "id": "subscription-site",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "subscription_resource_allowlist": ["group:1"],
            "require_subscription_expiry": True,
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [
            {
                "id": 1,
                "name": "expired",
                "status": "active",
                "platform": "openai",
                "subscription_type": "subscription",
                "rate_multiplier": 1,
            }
        ]
        active = [
            {
                "group_id": 1,
                "status": "active",
                "starts_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-07-11T23:59:59Z",
            }
        ]
        with mock.patch.object(
            client, "_request", side_effect=[groups, active, {}]
        ), mock.patch.object(client, "_list_keys", return_value=([], {})), mock.patch(
            "upstream_reconciler.clients._utc_now",
            return_value=datetime(2026, 7, 12, tzinfo=UTC),
        ):
            snapshot = client.scan()
        self.assertEqual(snapshot.resources, [])

    def test_allowlisted_subscription_type_drift_does_not_become_metered(self) -> None:
        config = {
            "id": "subscription-site",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "subscription_resource_allowlist": ["group:1"],
            "require_subscription_expiry": True,
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [
            {
                "id": 1,
                "name": "used-to-be-subscription",
                "status": "active",
                "platform": "openai",
                "subscription_type": "standard",
                "rate_multiplier": 0.22,
            },
            {
                "id": 2,
                "name": "ordinary-metered",
                "status": "active",
                "platform": "openai",
                "subscription_type": "standard",
                "rate_multiplier": 0.08,
            },
        ]
        with mock.patch.object(
            client, "_request", side_effect=[groups, [], {}]
        ), mock.patch.object(client, "_list_keys", return_value=([], {})):
            snapshot = client.scan()
        self.assertEqual([item.resource_id for item in snapshot.resources], ["group:2"])

    def test_allowlisted_subscription_rejects_timezone_less_expiry(self) -> None:
        config = {
            "id": "subscription-site",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "subscription_resource_allowlist": ["group:1"],
            "require_subscription_expiry": True,
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [
            {
                "id": 1,
                "name": "approved",
                "status": "active",
                "platform": "openai",
                "subscription_type": "subscription",
                "rate_multiplier": 1,
            }
        ]
        active = [
            {
                "group_id": 1,
                "status": "active",
                "starts_at": "2026-01-01T00:00:00Z",
                "expires_at": "2027-01-01T00:00:00",
            }
        ]
        with mock.patch.object(
            client, "_request", side_effect=[groups, active, {}]
        ), mock.patch.object(client, "_list_keys", return_value=([], {})):
            with self.assertRaisesRegex(ReconcileError, "timezone"):
                client.scan()

    def test_sub2api_excluded_group_ids_are_not_managed(self) -> None:
        config = {
            "id": "filtered-site",
            "type": "sub2api",
            "api_base": "https://example.test/api/v1",
            "include_platform": "openai",
            "exclude_group_ids": [2],
            "adopt": [],
        }
        client = Sub2APIProvider(config)
        groups = [
            {
                "id": 1,
                "name": "gpt",
                "status": "active",
                "platform": "openai",
                "subscription_type": "standard",
                "rate_multiplier": 0.15,
            },
            {
                "id": 2,
                "name": "glm",
                "status": "active",
                "platform": "openai",
                "subscription_type": "standard",
                "rate_multiplier": 0.15,
            },
        ]
        with mock.patch.object(
            client, "_request", side_effect=[groups, [], {}]
        ), mock.patch.object(client, "_list_keys", return_value=([], {})):
            snapshot = client.scan()
        self.assertEqual([item.resource_id for item in snapshot.resources], ["group:1"])

    def test_newapi_filter_excludes_non_gpt_groups(self) -> None:
        config = {
            "id": "new",
            "type": "new-api",
            "api_base": "https://example.test",
            "include_group_regex": r"^(?:gpt-|特惠分组$)",
        }
        client = NewAPIProvider(config)
        groups = {
            "gpt-low": {"ratio": 0.05},
            "特惠分组": {"ratio": 0.05},
            "claude-low": {"ratio": 0.01},
        }
        with mock.patch.object(client, "_request", return_value=groups), mock.patch.object(
            client, "_list_keys", return_value=([], {})
        ):
            snapshot = client.scan()
        self.assertEqual({item.group_ref for item in snapshot.resources}, {"gpt-low", "特惠分组"})

    def test_single_unclassified_group_does_not_trigger_schema_repair(self) -> None:
        client = NewAPIProvider(
            {
                "id": "new",
                "type": "new-api",
                "api_base": "https://example.test",
                "include_group_regex": "^gpt",
            }
        )
        groups = {"gpt-low": {"renamed_ratio": "sensitive-value"}}
        with mock.patch.object(client, "_request", return_value=groups), mock.patch.object(
            client, "_list_keys", return_value=([], {})
        ):
            with self.assertRaises(ReconcileError) as raised:
                client.scan()
        self.assertEqual(raised.exception.code, "unclassified_group")
        self.assertNotIn("schema_fingerprint", raised.exception.context)

    def test_root_shape_change_reports_only_sanitized_fingerprint(self) -> None:
        client = NewAPIProvider(
            {
                "id": "new",
                "type": "new-api",
                "api_base": "https://example.test",
                "include_group_regex": "^gpt",
            }
        )
        with mock.patch.object(client, "_request", return_value=[{"secret": "value"}]), mock.patch.object(
            client, "_list_keys", return_value=([], {})
        ):
            with self.assertRaises(ReconcileError) as raised:
                client.scan()
        self.assertEqual(raised.exception.code, "schema_changed")
        rendered = json.dumps(raised.exception.context)
        self.assertIn("schema_fingerprint", rendered)
        self.assertNotIn("value", rendered)

    def test_non_delete_not_found_is_not_classified_as_schema_change(self) -> None:
        response = SimpleNamespace(
            status_code=404,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/test",
            content=b"{}",
        )
        clients = [
            Sub2APIProvider(
                {
                    "id": "sub",
                    "type": "sub2api",
                    "api_base": "https://example.test/api/v1",
                }
            ),
            NewAPIProvider(
                {
                    "id": "new",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "include_group_regex": "^gpt",
                }
            ),
        ]
        for client in clients:
            with self.subTest(client=type(client).__name__), mock.patch.object(
                client, "_headers", return_value={}
            ), mock.patch.object(client.session, "request", return_value=response):
                with self.assertRaises(ReconcileError) as raised:
                    client._request("GET", "/api/test")
            self.assertEqual(raised.exception.code, "http_error")

    def test_newapi_normalizes_management_token_for_inference(self) -> None:
        self.assertEqual(NewAPIProvider._inference_secret("a" * 48), "sk-" + "a" * 48)
        self.assertEqual(NewAPIProvider._inference_secret("sk-abc"), "sk-abc")
        self.assertEqual(NewAPIProvider._inference_secret("abc***xyz"), "abc***xyz")

    def test_sub2api_probe_creation_uses_stable_idempotency_key(self) -> None:
        client = Sub2APIProvider(
            {
                "id": "sub",
                "type": "sub2api",
                "api_base": "https://example.test/api/v1",
            }
        )
        item = resource("sub", "group:9", "metered", "0.05")
        with mock.patch.object(
            client,
            "_request",
            return_value={
                "id": 7,
                "name": item.probe_marker,
                "group_id": 9,
                "status": "active",
                "key": "sk-probe",
            },
        ) as request:
            client.create_probe_key(item)
            client.create_probe_key(item)
        keys = [call.kwargs["idempotency_key"] for call in request.call_args_list]
        self.assertEqual(keys, [f"probe-{item.probe_marker}"] * 2)

    def test_provider_deletes_treat_not_found_as_already_deleted(self) -> None:
        response = SimpleNamespace(
            status_code=404,
            request=SimpleNamespace(method="DELETE"),
            url="https://example.test/key/7",
            content=b"",
        )
        clients = [
            Sub2APIProvider(
                {
                    "id": "sub",
                    "type": "sub2api",
                    "api_base": "https://example.test/api/v1",
                }
            ),
            NewAPIProvider(
                {
                    "id": "new",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "include_group_regex": "^gpt",
                }
            ),
        ]
        for client in clients:
            with self.subTest(client=type(client).__name__), mock.patch.object(
                client, "_headers", return_value={}
            ), mock.patch.object(client.session, "request", return_value=response):
                client.delete_key("7")

    def test_safe_reads_retry_bounded_rate_limits(self) -> None:
        client = NewAPIProvider(
            {
                "id": "new",
                "type": "new-api",
                "api_base": "https://example.test",
                "include_group_regex": "^gpt",
            }
        )
        limited = SimpleNamespace(
            status_code=429,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/test",
            content=b"{}",
        )
        success = SimpleNamespace(
            status_code=200,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/test",
            content=b'{"success":true,"data":{"ok":true}}',
            json=lambda: {"success": True, "data": {"ok": True}},
        )
        with mock.patch.object(client, "_headers", return_value={}), mock.patch.object(
            client.session, "request", side_effect=[limited, success]
        ) as request, mock.patch("upstream_reconciler.clients.time.sleep") as sleep:
            self.assertEqual(client._request("GET", "/api/test"), {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()


class CapabilityProbeTests(unittest.TestCase):
    @staticmethod
    def response(
        *,
        status: int = 200,
        url: str,
        payload: object | None = None,
        content_type: str = "application/json",
        lines: list[bytes] | None = None,
    ) -> SimpleNamespace:
        encoded = json.dumps(payload or {}).encode("utf-8")
        return SimpleNamespace(
            status_code=status,
            url=url,
            is_redirect=False,
            headers={"Content-Type": content_type},
            content=encoded,
            json=lambda: payload or {},
            iter_lines=lambda decode_unicode=False: iter(lines or []),
        )

    @staticmethod
    def terminal_line(
        *,
        model: str = REQUIRED_MODEL,
        response_id: str = "resp_probe",
        status: str = "completed",
    ) -> bytes:
        response: dict[str, object] = {
            "id": response_id,
            "object": "response",
            "model": model,
            "status": status,
            "output": [
                {
                    "id": "msg_probe",
                    "type": "message",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
            "usage": {"input_tokens": 4, "output_tokens": 1},
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return (
            "data: "
            + json.dumps(
                {"type": f"response.{status}", "response": response},
                separators=(",", ":"),
            )
        ).encode("utf-8")

    def test_model_selection_excludes_non_text_gpt_variants(self) -> None:
        model, count = select_codex_model(
            ["glm-5", "gpt-image-2", "gpt-4o-audio-preview", "gpt-5.6-sol"]
        )
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(count, 1)

    def test_models_catalog_without_any_codex_model_skips_paid_probe(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": "glm-5"}]},
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.code, "probe_no_codex_model")
        session.post.assert_not_called()

    def test_missing_models_endpoint_does_not_short_circuit_responses_probe(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            status=404,
            url="https://example.test/v1/models",
            payload={"error": {"message": "not found"}},
        )
        session.post.return_value = self.response(
            url="https://example.test/v1/responses",
            payload={},
            content_type="text/event-stream",
            lines=[
                self.terminal_line(),
            ],
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertTrue(result.compatible)
        session.post.assert_called_once()

    def test_probe_requires_real_responses_stream_success(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": "gpt-5.6-sol"}]},
        )
        session.post.return_value = self.response(
            url="https://example.test/v1/responses",
            payload={},
            content_type="text/event-stream",
            lines=[
                self.terminal_line(),
                b"data: [DONE]",
            ],
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.model, "gpt-5.6-sol")
        self.assertEqual(result.response_id, "resp_probe")
        session.post.assert_called_once()

    def test_probe_rejects_failed_completed_terminal_event(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": "gpt-5.6-sol"}]},
        )
        session.post.return_value = self.response(
            url="https://example.test/v1/responses",
            payload={},
            content_type="text/event-stream",
            lines=[
                b'data: {"type":"response.completed","response":{"id":"resp_probe","object":"response","model":"gpt-5.6-sol","status":"failed","error":{"code":"bad"}}}',
                b"data: [DONE]",
            ],
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.code, "probe_invalid_responses_protocol")

    def test_probe_rejects_failure_after_an_apparent_success_event(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": "gpt-5.6-sol"}]},
        )
        session.post.return_value = self.response(
            url="https://example.test/v1/responses",
            payload={},
            content_type="text/event-stream",
            lines=[
                self.terminal_line(),
                b'event: response.failed',
                b'data: {"type":"response.failed","response":{"status":"failed"}}',
            ],
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.code, "probe_invalid_responses_protocol")

    def test_probe_rejects_terminal_response_for_a_fallback_model(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": "gpt-5.5"}]},
        )
        session.post.return_value = self.response(
            url="https://example.test/v1/responses",
            payload={},
            content_type="text/event-stream",
            lines=[
                self.terminal_line(model="gpt-5.5"),
            ],
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.code, "probe_invalid_responses_protocol")
        self.assertEqual(
            session.post.call_args.kwargs["json"]["model"], REQUIRED_MODEL
        )

    def test_probe_rejects_terminal_event_without_full_response_contract(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": REQUIRED_MODEL}]},
        )
        session.post.return_value = self.response(
            url="https://example.test/v1/responses",
            payload={},
            content_type="text/event-stream",
            lines=[
                b'data: {"type":"response.completed","response":{"status":"completed"}}',
            ],
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.code, "probe_invalid_responses_protocol")

    def test_probe_rate_limit_is_retryable_and_not_compatible(self) -> None:
        session = mock.Mock()
        session.get.return_value = self.response(
            url="https://example.test/v1/models",
            payload={"data": [{"id": "gpt-5.6-sol"}]},
        )
        session.post.return_value = self.response(
            status=429,
            url="https://example.test/v1/responses",
            payload={"error": {"message": "quota"}},
        )
        result = probe_codex_responses(
            "https://example.test/v1", "sk-probe", session=session
        )
        self.assertFalse(result.compatible)
        self.assertTrue(result.retryable)
        self.assertEqual(result.http_status, 429)

    def test_provider_probe_ignores_legacy_fallback_preferences(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        key = UpstreamKey("7", item.probe_marker, "new", "active", "sk-probe")
        client = Sub2APIProvider(
            {
                "id": "a",
                "type": "sub2api",
                "api_base": "https://example.test/api/v1",
                "inference_base": "https://example.test/v1",
                "probe_preferred_models": ["gpt-5.5", "gpt-5.4"],
            }
        )
        with mock.patch(
            "upstream_reconciler.clients.probe_codex_responses",
            return_value=ProbeResult(True, "compatible", False, model=REQUIRED_MODEL),
        ) as probe:
            result = client.probe_resource(item, key)
        self.assertTrue(result.compatible)
        self.assertEqual(probe.call_args.kwargs["required_model"], REQUIRED_MODEL)
        self.assertNotIn("preferred_models", probe.call_args.kwargs)

    def test_sub2api_refresh_falls_back_to_stored_api_login(self) -> None:
        client = Sub2APIProvider(
            {
                "id": "sub",
                "type": "sub2api",
                "api_base": "https://example.test/api/v1",
            }
        )
        unauthorized = SimpleNamespace(
            status_code=401,
            headers={},
            request=SimpleNamespace(method="POST"),
            url="https://example.test/api/v1/auth/refresh",
            content=b"{}",
        )
        with mock.patch(
            "upstream_reconciler.clients.keychain_get", return_value="stale-refresh"
        ), mock.patch.object(
            client.session, "post", return_value=unauthorized
        ), mock.patch.object(client, "_login_from_keychain") as login:
            client._refresh()
        login.assert_called_once_with()

    def test_sub2api_invalid_refresh_payload_falls_back_to_stored_api_login(self) -> None:
        client = Sub2APIProvider(
            {
                "id": "sub",
                "type": "sub2api",
                "api_base": "https://example.test/api/v1",
            }
        )
        malformed = SimpleNamespace(
            status_code=200,
            headers={},
            request=SimpleNamespace(method="POST"),
            url="https://example.test/api/v1/auth/refresh",
            content=b'{"success":true,"data":{}}',
            json=lambda: {"success": True, "data": {}},
        )
        with mock.patch(
            "upstream_reconciler.clients.keychain_get", return_value="stale-refresh"
        ), mock.patch.object(
            client.session, "post", return_value=malformed
        ), mock.patch.object(client, "_login_from_keychain") as login:
            client._refresh()
        login.assert_called_once_with()

    def test_sub2api_forbidden_session_refreshes_and_retries(self) -> None:
        client = Sub2APIProvider(
            {
                "id": "sub",
                "type": "sub2api",
                "api_base": "https://example.test/api/v1",
            }
        )
        forbidden = SimpleNamespace(
            status_code=403,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/v1/groups/available",
            content=b"{}",
        )
        success = SimpleNamespace(
            status_code=200,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/v1/groups/available",
            content=b'{"data":[]}',
            json=lambda: {"data": []},
        )
        with mock.patch.object(client, "_headers", return_value={}), mock.patch.object(
            client, "_refresh"
        ) as refresh, mock.patch.object(
            client.session, "request", side_effect=[forbidden, success]
        ):
            self.assertEqual(client._request("GET", "/groups/available"), [])
        refresh.assert_called_once_with()

    def test_newapi_expired_session_reauthenticates_and_retries(self) -> None:
        client = NewAPIProvider(
            {
                "id": "new",
                "type": "new-api",
                "api_base": "https://example.test",
                "include_group_regex": "^gpt",
            }
        )
        unauthorized = SimpleNamespace(
            status_code=401,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/test",
            content=b"{}",
        )
        success = SimpleNamespace(
            status_code=200,
            headers={},
            request=SimpleNamespace(method="GET"),
            url="https://example.test/api/test",
            content=b'{"success":true,"data":{"ok":true}}',
            json=lambda: {"success": True, "data": {"ok": True}},
        )
        with mock.patch.object(client, "_headers", return_value={}), mock.patch.object(
            client, "_login_from_keychain"
        ) as login, mock.patch.object(
            client.session, "request", side_effect=[unauthorized, success]
        ) as request:
            self.assertEqual(client._request("GET", "/api/test"), {"ok": True})
        login.assert_called_once_with()
        self.assertEqual(request.call_count, 2)

    def test_login_rejection_is_classified_as_interactive_auth(self) -> None:
        client = Sub2APIProvider(
            {
                "id": "sub",
                "type": "sub2api",
                "api_base": "https://example.test/api/v1",
            }
        )
        rejected = SimpleNamespace(
            status_code=400,
            request=SimpleNamespace(method="POST"),
            url="https://example.test/api/v1/auth/login",
            content=b"{}",
        )
        with mock.patch.object(client.session, "post", return_value=rejected):
            with self.assertRaises(ReconcileError) as raised:
                client.login_with_credentials("account", "password")
        self.assertEqual(raised.exception.code, "interactive_auth_required")
        self.assertEqual(raised.exception.context, {"provider_id": "sub"})

    def test_newapi_non_json_login_is_classified_as_interactive_auth(self) -> None:
        client = NewAPIProvider(
            {
                "id": "new",
                "type": "new-api",
                "api_base": "https://example.test",
                "include_group_regex": "^gpt",
            }
        )
        captcha = SimpleNamespace(
            status_code=200,
            request=SimpleNamespace(method="POST"),
            url="https://example.test/api/user/login",
            content=b"<html>captcha</html>",
            json=mock.Mock(side_effect=ValueError("not json")),
        )
        with mock.patch.object(client.session, "post", return_value=captcha):
            with self.assertRaises(ReconcileError) as raised:
                client.login_with_credentials("account", "password")
        self.assertEqual(raised.exception.code, "interactive_auth_required")
        self.assertEqual(raised.exception.context, {"provider_id": "new"})


class NotificationTests(unittest.TestCase):
    def test_auth_notification_is_sent_once_and_deduplicated(self) -> None:
        error = ReconcileError(
            "interactive_auth_required",
            "safe",
            context={"provider_id": "code-plan"},
        )
        runner = mock.Mock(
            return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            atomic_write_json(
                config_path,
                {
                    "notifications": {
                        "telegram_command": ["python3", "notify.py"],
                        "telegram_text_flag": None,
                        "error_codes": ["interactive_auth_required"],
                        "dedupe_hours": 24,
                    }
                },
            )
            now = datetime(2026, 7, 12, tzinfo=UTC)
            first = notify_reconcile_error(
                error,
                config_path=config_path,
                state_dir=root / "state",
                now=now,
                runner=runner,
            )
            second = notify_reconcile_error(
                error,
                config_path=config_path,
                state_dir=root / "state",
                now=now + timedelta(hours=1),
                runner=runner,
            )
            stored = (root / "state" / "notification-state.json").read_text()
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "deduplicated")
        runner.assert_called_once()
        self.assertNotIn("--text", runner.call_args.args[0])
        self.assertNotIn("password", stored)
        self.assertNotIn("code-plan", stored)

    def test_non_auth_error_does_not_send_notification(self) -> None:
        runner = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            atomic_write_json(
                config_path,
                {
                    "notifications": {
                        "telegram_command": ["python3", "notify.py"],
                        "error_codes": ["auth_required"],
                    }
                },
            )
            result = notify_reconcile_error(
                ReconcileError("rate_limited", "safe"),
                config_path=config_path,
                state_dir=root / "state",
                runner=runner,
            )
        self.assertEqual(result["status"], "not_applicable")
        runner.assert_not_called()

    def test_wrapped_partial_mutation_preserves_auth_notification(self) -> None:
        runner = mock.Mock(
            return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        error = ReconcileError(
            "partial_external_mutation",
            "safe",
            context={
                "cause_code": "interactive_auth_required",
                "provider_id": "yjy",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            atomic_write_json(
                config_path,
                {
                    "notifications": {
                        "telegram_command": ["notify"],
                        "error_codes": ["interactive_auth_required"],
                    }
                },
            )
            result = notify_reconcile_error(
                error,
                config_path=config_path,
                state_dir=root / "state",
                runner=runner,
            )
        self.assertEqual(result["status"], "sent")
        self.assertIn("interactive_auth_required", runner.call_args.args[0][-1])


class PlanTests(unittest.TestCase):
    def test_create_intent_recovers_orphan_probe_key_after_group_disappears(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        key = UpstreamKey("7", item.probe_marker, "new", "inactive", "sk-***")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot(
            "a", [], [key], {"7": {}}
        )
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "probe_new_resources": True,
                    "adopt": [],
                }
            ]
        }
        previous_pending = {
            "schema": 1,
            "run_id": "interrupted-2",
            "recovery_chain": [
                {
                    "run_id": "interrupted-1",
                    "mutations": [
                        {
                            "at": "2026-07-12T00:00:00+00:00",
                            "phase": "intent",
                            "kind": "create_probe_key",
                            "provider_id": "a",
                            "resource_id": "group:new",
                            "marker": item.probe_marker,
                        }
                    ],
                }
            ],
            "mutations": [],
        }
        state = {
            "schema": 1,
            "resources": {
                "a/group:new": {
                    "provider_id": "a",
                    "resource_id": "group:new",
                    "marker": item.probe_marker,
                    "upstream_key_id": "6",
                    "target_account_id": None,
                    "status": "upstream_key_deleted_target_retained",
                }
            },
            "candidate_probes": {
                "a/group:new": {
                    "outcome": "retry",
                    "upstream_key_id": "6",
                }
            },
        }
        persisted: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ):
            recovered = _recover_pending_probe_keys(
                config,
                state,
                previous_pending,
                audit_path=Path(tmp) / "audit.jsonl",
                persist_state=lambda value: persisted.append(
                    json.loads(json.dumps(value))
                ),
            )
        self.assertEqual(
            recovered,
            [
                {
                    "provider_id": "a",
                    "resource_id": "group:new",
                    "upstream_key_id": "7",
                    "group_present": False,
                }
            ],
        )
        entry = state["resources"]["a/group:new"]
        self.assertEqual(entry["status"], "probe_pending")
        self.assertEqual(entry["upstream_key_id"], "7")
        self.assertEqual(entry["marker"], item.probe_marker)
        self.assertEqual(
            state["candidate_probes"]["a/group:new"]["code"],
            "probe_recovery_pending",
        )
        self.assertEqual(len(persisted), 1)
        provider.delete_key.assert_not_called()
        provider.create_probe_key.assert_not_called()

    def test_maintenance_safe_plan_rejects_fresh_destructive_drift(self) -> None:
        with self.assertRaises(ReconcileError) as destructive:
            _assert_maintenance_safe_plan(
                {
                    "resources": [{}, {}],
                    "actions": [{"kind": "quarantine_missing_resource"}],
                },
                min_active_resources=2,
            )
        self.assertEqual(destructive.exception.code, "maintenance_plan_unsafe")

        with self.assertRaises(ReconcileError) as reduced:
            _assert_maintenance_safe_plan(
                {"resources": [{}], "actions": []},
                min_active_resources=2,
            )
        self.assertEqual(reduced.exception.code, "maintenance_plan_unsafe")

    def test_maintenance_safe_apply_checks_locked_plan_before_target_writes(self) -> None:
        target = mock.Mock()
        target.list_accounts.return_value = []
        inventory = Inventory({}, {}, target, [], [], {})
        initial_plan = {
            "schema": 1,
            "phase": "candidate_qualification",
            "observed_hash": "baseline",
            "resources": [],
            "skipped_resources": [],
            "actions": [],
            "summary": {},
        }
        unsafe_plan = {
            "schema": 1,
            "observed_hash": "fresh",
            "resources": [{}],
            "skipped_resources": [],
            "actions": [{"kind": "delete_upstream_key"}],
            "summary": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with mock.patch(
                "upstream_reconciler.runtime.load_config",
                return_value={"providers": []},
            ), mock.patch(
                "upstream_reconciler.runtime.default_state_dir", return_value=state_dir
            ), mock.patch(
                "upstream_reconciler.runtime._prequalification_context",
                return_value=(initial_plan, inventory),
            ), mock.patch(
                "upstream_reconciler.runtime._qualify_pending_resources",
                return_value=[],
            ), mock.patch(
                "upstream_reconciler.runtime.build_plan",
                return_value=(unsafe_plan, inventory),
            ):
                with self.assertRaises(ReconcileError) as raised:
                    reconcile_apply(
                        maintenance_safe=True,
                        min_active_resources=2,
                    )
            self.assertFalse((state_dir / "pending-run.json").exists())
        self.assertEqual(raised.exception.code, "maintenance_plan_unsafe")
        target.create_account.assert_not_called()
        target.bulk_update.assert_not_called()
        target.set_schedulable.assert_not_called()

    def test_apply_refreshes_snapshot_before_first_adoption_routing_mutation(self) -> None:
        item = resource("a", "group:15", "metered", "0.15")
        assign_priorities([item])
        item.key = UpstreamKey("1", item.marker, "15", "active", "sk-current")
        account = {
            "id": 10,
            "name": "adopt-me",
            "platform": "openai",
            "type": "apikey",
            "priority": item.priority,
            "concurrency": 50,
            "schedulable": True,
            "credentials": {"base_url": "https://a.example/v1"},
            "extra": {},
        }
        target = mock.Mock()
        target.list_accounts.return_value = [account]
        provider = mock.Mock()
        provider.config = {"inference_base": "https://a.example/v1"}
        pre_inventory = Inventory({}, {}, target, [account], [], {})
        full_inventory = Inventory(
            {"a": provider},
            {},
            target,
            [account],
            [Binding(item, account, adopted=True)],
            {},
        )
        pre_plan = {
            "schema": 1,
            "phase": "candidate_qualification",
            "observed_hash": "pre",
            "resources": [],
            "skipped_resources": [],
            "actions": [],
            "summary": {},
        }
        full_plan = {
            "schema": 1,
            "observed_hash": "full",
            "resources": [{"target_account_id": 10}],
            "skipped_resources": [],
            "actions": [],
            "summary": {},
        }
        config = {
            "target": {"group_id": 9, "concurrency": 100},
            "providers": [
                {
                    "id": "a",
                    "inference_base": "https://a.example/v1",
                    "target_concurrency": 8,
                }
            ],
        }

        def mutate_routing(*_args: object, **_kwargs: object) -> None:
            target.bulk_update([10], concurrency=8)

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with mock.patch(
                "upstream_reconciler.runtime.load_config", return_value=config
            ), mock.patch(
                "upstream_reconciler.runtime.default_state_dir", return_value=state_dir
            ), mock.patch(
                "upstream_reconciler.runtime._prequalification_context",
                return_value=(pre_plan, pre_inventory),
            ), mock.patch(
                "upstream_reconciler.runtime._qualify_pending_resources",
                return_value=[],
            ), mock.patch(
                "upstream_reconciler.runtime.build_plan",
                return_value=(full_plan, full_inventory),
            ), mock.patch(
                "upstream_reconciler.runtime._apply_probe_deferred_resources"
            ), mock.patch(
                "upstream_reconciler.runtime._apply_missing_resources"
            ), mock.patch(
                "upstream_reconciler.runtime._apply_active_resources",
                side_effect=mutate_routing,
            ), mock.patch(
                "upstream_reconciler.runtime._verify_active",
                side_effect=ReconcileError("verification_failed", "simulated"),
            ):
                with self.assertRaises(ReconcileError) as raised:
                    reconcile_apply()

            snapshot_path = next((state_dir / "snapshots").glob("*.json"))
            snapshot = json.loads(snapshot_path.read_text())
        self.assertEqual(raised.exception.code, "verification_failed")
        self.assertEqual(snapshot["accounts"][0]["id"], 10)
        self.assertEqual(snapshot["accounts"][0]["concurrency"], 50)
        target.bulk_update.assert_any_call([10], concurrency=8)
        target.bulk_update.assert_any_call([10], concurrency=50)

    def test_probe_crash_is_journaled_and_provisional_state_is_durable(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        key = UpstreamKey("7", item.probe_marker, "new", "active", "sk-probe")
        provider = mock.Mock()
        provider.provider_id = "a"
        provider.scan.return_value = ProviderSnapshot("a", [item], [], {})
        provider.create_probe_key.return_value = key
        provider.probe_resource.side_effect = RuntimeError("simulated crash")
        target = mock.Mock()
        target.list_accounts.return_value = []
        pre_plan = {
            "schema": 1,
            "phase": "candidate_qualification",
            "observed_hash": "baseline",
            "resources": [],
            "skipped_resources": [],
            "actions": [],
            "summary": {
                "providers": 1,
                "resources": 0,
                "subscriptions": 0,
                "metered": 0,
                "probe_deferred": 0,
                "actions": 0,
            },
        }
        pre_inventory = Inventory({}, {}, target, [], [], {})
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "probe_new_resources": True,
                    "adopt": [],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with mock.patch(
                "upstream_reconciler.runtime.load_config", return_value=config
            ), mock.patch(
                "upstream_reconciler.runtime.default_state_dir", return_value=state_dir
            ), mock.patch(
                "upstream_reconciler.runtime._prequalification_context",
                return_value=(pre_plan, pre_inventory),
            ), mock.patch(
                "upstream_reconciler.runtime.provider_from_config",
                return_value=provider,
            ), mock.patch("upstream_reconciler.runtime.keychain_set"):
                with self.assertRaises(ReconcileError) as raised:
                    reconcile_apply()

            pending = json.loads((state_dir / "pending-run.json").read_text())
            durable = json.loads((state_dir / "state.json").read_text())
        self.assertEqual(raised.exception.code, "partial_external_mutation")
        sequence = [
            (event["phase"], event["kind"])
            for event in pending["mutations"]
        ]
        self.assertEqual(
            sequence,
            [
                ("intent", "create_probe_key"),
                ("done", "create_probe_key"),
                ("intent", "probe_upstream_resource"),
            ],
        )
        rendered = json.dumps(pending)
        self.assertNotIn("sk-probe", rendered)
        self.assertEqual(
            durable["candidate_probes"]["a/group:new"]["outcome"], "pending"
        )
        self.assertEqual(
            durable["resources"]["a/group:new"]["status"], "probe_pending"
        )
        target.create_account.assert_not_called()

    def test_observed_deferred_candidate_is_not_treated_as_missing(self) -> None:
        state_id = "a/group:new"
        entry = {
            "provider_id": "a",
            "resource_id": "group:new",
            "marker": marker_for("a", "group:new"),
            "upstream_key_id": "7",
            "target_account_id": 9,
            "missing_count": 5,
            "missing_since": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
            "status": "probe_deferred",
        }
        account = {"id": 9, "schedulable": True}
        target = mock.Mock()
        provider = mock.Mock()
        inventory = Inventory(
            providers={"a": provider},
            snapshots={"a": ProviderSnapshot("a", [], [])},
            target=target,
            target_accounts=[account],
            bindings=[],
            settings={},
            skipped_resources=[
                {
                    "provider_id": "a",
                    "resource_id": "group:new",
                    "reason": "probe_no_codex_model",
                    "target_account_id": 9,
                }
            ],
        )
        state = {"resources": {state_id: entry}, "candidate_probes": {}}
        actions = _action_plan(
            inventory,
            state,
            {
                "delete_upstream_keys": True,
                "delete_grace_hours": 24,
                "delete_min_confirmations": 3,
            },
        )
        self.assertNotIn(
            "quarantine_missing_resource", [item.kind for item in actions]
        )
        self.assertNotIn("delete_upstream_key", [item.kind for item in actions])

        _apply_probe_deferred_resources(inventory, state)
        _apply_missing_resources(
            {
                "delete_upstream_keys": True,
                "delete_grace_hours": 24,
                "delete_min_confirmations": 3,
            },
            inventory,
            state,
        )
        self.assertEqual(entry["missing_count"], 0)
        self.assertIsNone(entry["missing_since"])
        self.assertEqual(entry["status"], "probe_deferred")
        target.set_schedulable.assert_called_once_with(9, False)
        provider.delete_key.assert_not_called()

    def test_stale_compatible_record_cannot_pass_live_key_validation(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        provider_config = {
            "id": "a",
            "inference_base": "https://a.example/v1",
            "probe_new_resources": True,
            "adopt": [],
        }
        state = {
            "resources": {
                "a/group:new": {
                    "provider_id": "a",
                    "resource_id": "group:new",
                    "status": "probe_compatible",
                    "target_account_id": None,
                    "upstream_key_id": "7",
                }
            },
            "candidate_probes": {
                "a/group:new": {
                    "provider_id": "a",
                    "resource_id": "group:new",
                    "outcome": "compatible",
                    "probe_policy": PROBE_POLICY,
                    "response_contract": RESPONSE_CONTRACT,
                    "required_model": REQUIRED_MODEL,
                    "policy_hash": _probe_policy_hash(provider_config),
                    "inference_base": "https://a.example/v1",
                    "model": REQUIRED_MODEL,
                    "response_id": "resp-stale-proof",
                    "group_ref": "new",
                    "marker": item.marker,
                    "upstream_key_id": "7",
                    "key_fingerprint": fingerprint_secret("sk-old"),
                }
            },
        }
        snapshot = ProviderSnapshot(
            "a",
            [item],
            [UpstreamKey("7", item.marker, "new", "active", "sk-replaced")],
        )
        allowed, reason = _resource_probe_gate(
            provider_config,
            item,
            state,
            snapshot=snapshot,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "probe_key_changed")

    def test_masked_probe_key_is_revealed_once_then_reused_from_keychain(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        masked_probe = UpstreamKey("7", item.probe_marker, "new", "active", "sk-***")
        masked_formal = UpstreamKey("7", item.marker, "new", "active", "sk-***")
        provider = mock.Mock()
        provider.provider_id = "a"
        provider.scan.side_effect = [
            ProviderSnapshot("a", [item], [masked_probe], {"7": {}}),
            ProviderSnapshot("a", [item], [masked_formal], {"7": {}}),
        ]
        provider.reveal_key.return_value = UpstreamKey(
            "7", item.probe_marker, "new", "active", "sk-full-probe"
        )
        provider.probe_resource.return_value = ProbeResult(
            True,
            "compatible",
            False,
            http_status=200,
            model="gpt-5.6-sol",
            model_count=1,
            response_id="resp-masked-key",
        )
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "probe_new_resources": True,
                    "adopt": [],
                }
            ]
        }
        state = {"schema": 1, "resources": {}}
        vault: dict[str, str] = {}

        def fake_get(account: str, *, required: bool = True) -> str | None:
            value = vault.get(account)
            if value is None and required:
                raise AssertionError(f"unexpected required Keychain read: {account}")
            return value

        def fake_set(account: str, value: str) -> None:
            vault[account] = value

        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ), mock.patch(
            "upstream_reconciler.runtime.keychain_get", side_effect=fake_get
        ), mock.patch(
            "upstream_reconciler.runtime.keychain_set", side_effect=fake_set
        ):
            first = _qualify_pending_resources(
                config,
                state,
                audit_path=Path(tmp) / "audit.jsonl",
                record_mutation=events.append,
            )
            second = _qualify_pending_resources(
                config,
                state,
                audit_path=Path(tmp) / "audit.jsonl",
                record_mutation=events.append,
            )
        self.assertEqual([item["compatible"] for item in first], [True])
        self.assertEqual(second, [])
        provider.reveal_key.assert_called_once()
        provider.probe_resource.assert_called_once()
        provider.rename_key.assert_called_once()
        provider.create_probe_key.assert_not_called()
        self.assertEqual(
            state["candidate_probes"]["a/group:new"]["attempt_count"], 1
        )
        self.assertEqual(
            [event["kind"] for event in events].count("reveal_probe_key"), 2
        )

    def test_disappeared_candidate_cleans_only_owned_probe_key_after_grace(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        probe_key = UpstreamKey("7", item.probe_marker, "new", "inactive", "sk-probe")
        unrelated = UpstreamKey("8", "personal", "other", "active", "sk-other")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot(
            "a", [], [probe_key, unrelated], {"7": {}, "8": {}}
        )
        inventory = Inventory(
            providers={"a": provider},
            snapshots={
                "a": ProviderSnapshot(
                    "a", [], [probe_key, unrelated], {"7": {}, "8": {}}
                )
            },
            target=mock.Mock(),
            target_accounts=[],
            bindings=[],
            settings={},
        )
        state_id = "a/group:new"
        state = {
            "resources": {
                state_id: {
                    "provider_id": "a",
                    "resource_id": "group:new",
                    "marker": item.probe_marker,
                    "upstream_key_id": "7",
                    "target_account_id": None,
                    "missing_count": 3,
                    "missing_since": (
                        datetime.now(UTC) - timedelta(hours=25)
                    ).isoformat(),
                    "status": "quarantined",
                }
            },
            "candidate_probes": {state_id: {"outcome": "retry"}},
        }
        config = {
            "delete_upstream_keys": True,
            "delete_grace_hours": 24,
            "delete_min_confirmations": 3,
        }
        events: list[dict[str, object]] = []
        _apply_missing_resources(
            config,
            inventory,
            state,
            increment=False,
            allow_delete=True,
            planned_deletions={state_id: "delete_upstream_key"},
            record_mutation=events.append,
        )
        provider.delete_key.assert_called_once_with("7")
        self.assertNotIn(state_id, state["candidate_probes"])
        self.assertEqual(
            state["resources"][state_id]["status"],
            "upstream_key_deleted_target_retained",
        )
        self.assertEqual(
            [event["kind"] for event in events],
            ["delete_upstream_key", "delete_upstream_key"],
        )

    def test_failed_new_group_probe_is_deferred_and_retried_next_apply(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        key = UpstreamKey("7", item.probe_marker, "new", "active", "sk-probe")
        provider = mock.Mock()
        provider.provider_id = "a"
        provider.scan.side_effect = [
            ProviderSnapshot("a", [item], [], {}),
            ProviderSnapshot("a", [item], [key], {"7": {}}),
        ]
        provider.create_probe_key.return_value = key
        provider.probe_resource.return_value = ProbeResult(
            False,
            "probe_no_codex_model",
            True,
            http_status=200,
        )
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "probe_new_resources": True,
                    "adopt": [],
                }
            ]
        }
        state = {"schema": 1, "resources": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ):
            first = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
            second = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
        self.assertEqual([result["compatible"] for result in first], [False])
        self.assertEqual([result["compatible"] for result in second], [False])
        self.assertEqual(provider.probe_resource.call_count, 2)
        self.assertEqual(provider.create_probe_key.call_count, 1)
        self.assertEqual(provider.disable_key.call_count, 2)
        allowed, reason = _resource_probe_gate(config["providers"][0], item, state)
        self.assertFalse(allowed)
        self.assertEqual(reason, "probe_no_codex_model")

    def test_successful_new_group_probe_promotes_key_before_relay_plan(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        key = UpstreamKey("7", item.probe_marker, "new", "active", "sk-probe")
        provider = mock.Mock()
        provider.provider_id = "a"
        provider.scan.return_value = ProviderSnapshot("a", [item], [], {})
        provider.create_probe_key.return_value = key
        provider.probe_resource.return_value = ProbeResult(
            True,
            "compatible",
            False,
            http_status=200,
            model="gpt-5.6-sol",
            model_count=3,
            response_id="resp-new-group",
        )
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "probe_new_resources": True,
                    "adopt": [],
                }
            ]
        }
        state = {"schema": 1, "resources": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ):
            results = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
        self.assertEqual([result["compatible"] for result in results], [True])
        provider.rename_key.assert_called_once_with(key, item.marker)
        provider.disable_key.assert_not_called()
        allowed, reason = _resource_probe_gate(config["providers"][0], item, state)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_new_group_probe_is_mandatory_without_legacy_toggle(self) -> None:
        item = resource("a", "group:new", "metered", "0.05")
        key = UpstreamKey("7", item.probe_marker, "new", "active", "sk-probe")
        provider = mock.Mock()
        provider.provider_id = "a"
        provider.scan.return_value = ProviderSnapshot("a", [item], [], {})
        provider.create_probe_key.return_value = key
        provider.probe_resource.return_value = ProbeResult(
            True,
            "compatible",
            False,
            http_status=200,
            model=REQUIRED_MODEL,
            model_count=0,
        )
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "adopt": [],
                }
            ]
        }
        state = {"schema": 1, "resources": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ), mock.patch("upstream_reconciler.runtime.keychain_set"):
            results = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
        self.assertEqual([item["compatible"] for item in results], [True])
        provider.probe_resource.assert_called_once()

    def test_historical_route_is_not_revalidated_or_stopped_by_default(self) -> None:
        item = resource("a", "group:old", "metered", "0.05")
        key = UpstreamKey("7", item.marker, "old", "active", "sk-existing")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot("a", [item], [key], {})
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "adopt": [],
                }
            ]
        }
        state = {
            "schema": 1,
            "resources": {
                "a/group:old": {
                    "provider_id": "a",
                    "resource_id": "group:old",
                    "status": "active",
                    "upstream_key_id": "7",
                    "target_account_id": 12,
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ):
            results = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
        self.assertEqual(results, [])
        self.assertEqual(state["resources"]["a/group:old"]["status"], "active")
        provider.probe_resource.assert_not_called()
        provider.disable_key.assert_not_called()

    def test_historical_revalidation_failure_preserves_existing_route(self) -> None:
        item = resource("a", "group:old", "metered", "0.05")
        key = UpstreamKey("7", item.marker, "old", "active", "sk-existing")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot("a", [item], [key], {})
        provider.probe_resource.return_value = ProbeResult(
            False,
            "probe_responses_rejected",
            True,
            http_status=400,
            model=REQUIRED_MODEL,
            model_count=1,
            response_id="resp-historical-refresh",
        )
        config = {
            "providers": [
                {
                    "id": "a",
                    "type": "sub2api",
                    "inference_base": "https://a.example/v1",
                    "probe_revalidate_resource_ids": ["group:old"],
                    "adopt": [],
                }
            ]
        }
        state = {
            "schema": 1,
            "resources": {
                "a/group:old": {
                    "provider_id": "a",
                    "resource_id": "group:old",
                    "status": "active",
                    "upstream_key_id": "7",
                    "target_account_id": 12,
                    "key_fingerprint": key.fingerprint,
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ), mock.patch("upstream_reconciler.runtime.keychain_set"):
            results = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
        self.assertFalse(results[0]["compatible"])
        self.assertTrue(results[0]["routing_preserved"])
        self.assertEqual(state["resources"]["a/group:old"]["status"], "active")
        proof = state["candidate_probes"]["a/group:old"]
        self.assertEqual(proof["required_model"], REQUIRED_MODEL)
        self.assertEqual(proof["mode"], "historical_revalidation")
        provider.disable_key.assert_not_called()
        provider.rename_key.assert_not_called()
        provider.create_probe_key.assert_not_called()

    def test_historical_proof_reuse_and_policy_invalidation_are_safe(self) -> None:
        item = resource("a", "group:old", "metered", "0.05")
        key = UpstreamKey("7", item.marker, "old", "active", "sk-existing")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot("a", [item], [key], {})
        provider.probe_resource.return_value = ProbeResult(
            True,
            "compatible",
            False,
            http_status=200,
            model=REQUIRED_MODEL,
            model_count=1,
        )
        provider_config = {
            "id": "a",
            "type": "sub2api",
            "inference_base": "https://a.example/v1",
            "probe_revalidate_resource_ids": ["group:old"],
            "adopt": [],
        }
        proof = {
            "provider_id": "a",
            "resource_id": "group:old",
            "probe_policy": PROBE_POLICY,
            "response_contract": RESPONSE_CONTRACT,
            "required_model": REQUIRED_MODEL,
            "policy_hash": _probe_policy_hash(provider_config),
            "inference_base": "https://a.example/v1",
            "outcome": "compatible",
            "model": REQUIRED_MODEL,
            "response_id": "resp-historical-proof",
            "upstream_key_id": "7",
            "key_fingerprint": key.fingerprint,
            "group_ref": "old",
            "marker": item.marker,
            "attempt_count": 1,
        }
        state = {
            "schema": 1,
            "resources": {
                "a/group:old": {
                    "provider_id": "a",
                    "resource_id": "group:old",
                    "status": "active",
                    "upstream_key_id": "7",
                    "target_account_id": 12,
                    "key_fingerprint": key.fingerprint,
                    "probe": dict(proof),
                }
            },
            "candidate_probes": {"a/group:old": proof},
        }
        config = {"providers": [provider_config]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "upstream_reconciler.runtime.provider_from_config",
            return_value=provider,
        ), mock.patch("upstream_reconciler.runtime.keychain_set"):
            reused = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
            state["candidate_probes"]["a/group:old"]["response_contract"] = "old"
            refreshed = _qualify_pending_resources(
                config, state, audit_path=Path(tmp) / "audit.jsonl"
            )
        self.assertEqual(reused, [])
        self.assertEqual([item["compatible"] for item in refreshed], [True])
        provider.probe_resource.assert_called_once()
        self.assertEqual(
            state["candidate_probes"]["a/group:old"]["response_contract"],
            RESPONSE_CONTRACT,
        )
        self.assertEqual(state["resources"]["a/group:old"]["status"], "active")
        provider.disable_key.assert_not_called()

    def test_initial_adoption_and_equal_priority_plan(self) -> None:
        first = resource("a", "group:15", "metered", "0.15")
        second = resource("b", "group:15", "metered", "0.150")
        assign_priorities([first, second])
        first.key = UpstreamKey("1", "old-a", "15", "active", "sk-a")
        second.key = UpstreamKey("2", second.marker, "15", "active", "sk-b")
        account_a = {"id": 10, "platform": "openai", "type": "apikey", "priority": 6, "schedulable": True, "extra": {}}
        account_b = {
            "id": 11,
            "platform": "openai",
            "type": "apikey",
            "priority": 5,
            "schedulable": True,
            "extra": {METADATA_KEY: desired_metadata(second)},
        }
        inventory = Inventory(
            providers={
                "a": SimpleNamespace(config={"inference_base": "https://a.example/v1"}),
                "b": SimpleNamespace(config={"inference_base": "https://b.example/v1"}),
            },
            snapshots={},
            target=mock.Mock(),
            target_accounts=[account_a, account_b],
            bindings=[Binding(first, account_a, adopted=True), Binding(second, account_b)],
            settings={},
        )
        actions = _action_plan(inventory, {"resources": {}})
        kinds = [item.kind for item in actions]
        self.assertIn("rename_upstream_key", kinds)
        self.assertIn("update_target_metadata", kinds)
        self.assertIn("rotate_target_credential", kinds)
        self.assertIn("update_target_priority", kinds)
        self.assertEqual(first.priority, second.priority)

    def test_initial_adoption_repairs_credentials_before_claiming_ownership(self) -> None:
        item = resource("a", "group:15", "metered", "0.15")
        assign_priorities([item])
        item.key = UpstreamKey("1", item.marker, "15", "active", "sk-current")
        account = {
            "id": 10,
            "platform": "openai",
            "type": "apikey",
            "priority": item.priority,
            "schedulable": True,
            "credentials": {"base_url": "https://old.example/v1"},
            "extra": {},
        }
        provider = mock.Mock()
        provider.config = {"inference_base": "https://a.example/v1"}
        target = mock.Mock()
        inventory = Inventory(
            providers={"a": provider},
            snapshots={},
            target=target,
            target_accounts=[account],
            bindings=[Binding(item, account, adopted=True)],
            settings={},
        )
        config = {
            "target": {"group_id": 9},
            "providers": [{"id": "a", "inference_base": "https://a.example/v1"}],
        }
        state = {"resources": {}}
        with mock.patch("upstream_reconciler.runtime.keychain_set"):
            _apply_active_resources(config, inventory, state)
        provider.reveal_key.assert_not_called()
        credential_calls = [
            call
            for call in target.bulk_update.call_args_list
            if "credentials" in call.kwargs
        ]
        self.assertEqual(len(credential_calls), 1)
        self.assertEqual(
            credential_calls[0].kwargs["credentials"],
            {"base_url": "https://a.example/v1", "api_key": "sk-current"},
        )

    def test_new_target_account_uses_provider_concurrency(self) -> None:
        item = resource("a", "group:15", "metered", "0.15")
        assign_priorities([item])
        item.key = UpstreamKey("1", item.marker, "15", "active", "sk-current")
        provider = mock.Mock()
        target = mock.Mock()
        target.create_account.return_value = {
            "id": 10,
            "priority": item.priority,
            "schedulable": False,
        }
        inventory = Inventory(
            providers={"a": provider},
            snapshots={},
            target=target,
            target_accounts=[],
            bindings=[Binding(item, None)],
            settings={},
        )
        config = {
            "target": {"group_id": 9, "concurrency": 100},
            "providers": [
                {
                    "id": "a",
                    "inference_base": "https://a.example/v1",
                    "target_concurrency": 8,
                }
            ],
        }
        actions = _action_plan(inventory, {"resources": {}}, config)
        create = next(action for action in actions if action.kind == "create_target_account")
        self.assertEqual(create.detail["concurrency"], 8)
        with mock.patch("upstream_reconciler.runtime.keychain_set"):
            _apply_active_resources(config, inventory, {"resources": {}})
        self.assertEqual(target.create_account.call_args.kwargs["concurrency"], 8)

    def test_existing_target_account_concurrency_drift_is_repaired(self) -> None:
        item = resource("a", "group:15", "metered", "0.15")
        assign_priorities([item])
        item.key = UpstreamKey("1", item.marker, "15", "active", "sk-current")
        account = {
            "id": 10,
            "priority": item.priority,
            "concurrency": 100,
            "schedulable": True,
            "credentials": {"base_url": "https://a.example/v1"},
            "extra": {METADATA_KEY: desired_metadata(item)},
        }
        provider = mock.Mock()
        provider.config = {"inference_base": "https://a.example/v1"}
        target = mock.Mock()
        inventory = Inventory(
            providers={"a": provider},
            snapshots={},
            target=target,
            target_accounts=[account],
            bindings=[Binding(item, account)],
            settings={},
        )
        config = {
            "target": {"group_id": 9, "concurrency": 100},
            "providers": [
                {
                    "id": "a",
                    "inference_base": "https://a.example/v1",
                    "target_concurrency": 8,
                }
            ],
        }
        actions = _action_plan(inventory, {"resources": {}}, config)
        self.assertIn("update_target_concurrency", [action.kind for action in actions])
        with mock.patch("upstream_reconciler.runtime.keychain_set"):
            _apply_active_resources(config, inventory, {"resources": {}})
        target.bulk_update.assert_any_call([10], concurrency=8)

    def test_concurrency_is_hashed_snapshotted_rolled_back_and_verified(self) -> None:
        item = resource("a", "group:15", "metered", "0.15")
        assign_priorities([item])
        item.key = UpstreamKey("1", item.marker, "15", "active", "sk-current")
        account = {
            "id": 10,
            "name": "managed-a",
            "priority": item.priority,
            "concurrency": 50,
            "schedulable": True,
            "credentials": {"base_url": "https://a.example/v1"},
            "extra": {METADATA_KEY: desired_metadata(item)},
        }
        provider = mock.Mock()
        provider.config = {"inference_base": "https://a.example/v1"}
        inventory = Inventory(
            providers={"a": provider},
            snapshots={},
            target=mock.Mock(),
            target_accounts=[account],
            bindings=[Binding(item, account)],
            settings={},
        )
        original_hash = _observed_hash(inventory)
        account["concurrency"] = 40
        self.assertNotEqual(_observed_hash(inventory), original_hash)
        account["concurrency"] = 50

        snapshot = _snapshot_payload(
            inventory,
            {"resources": {}},
            {"observed_hash": original_hash},
        )
        self.assertEqual(snapshot["accounts"][0]["concurrency"], 50)
        target = mock.Mock()
        self.assertEqual(_restore_target_routing(target, snapshot), [])
        target.bulk_update.assert_any_call([10], concurrency=50)

        config = {
            "target": {"group_id": 9, "concurrency": 100},
            "providers": [
                {
                    "id": "a",
                    "inference_base": "https://a.example/v1",
                    "target_concurrency": 8,
                }
            ],
        }
        with mock.patch("upstream_reconciler.runtime.build_inventory", return_value=inventory):
            with self.assertRaisesRegex(ReconcileError, "concurrency verification"):
                _verify_active(config, {"resources": {}})

    def test_missing_state_becomes_quarantine_not_delete(self) -> None:
        inventory = Inventory({}, {}, mock.Mock(), [], [], {})
        actions = _action_plan(
            inventory,
            {
                "resources": {
                    "a/group:gone": {
                        "target_account_id": 9,
                        "upstream_key_id": "3",
                        "missing_count": 0,
                    }
                }
            },
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "quarantine_missing_resource")

    def test_inactive_adoption_is_seeded_for_quarantine(self) -> None:
        marker = marker_for("a", "group:gone")
        snapshot = ProviderSnapshot(
            "a",
            [],
            [UpstreamKey("7", marker, "gone", "active", "sk-old")],
        )
        state_resources: dict[str, object] = {}
        account = {"id": 9, "platform": "openai", "type": "apikey", "priority": 3}
        _seed_inactive_adoptions(
            {
                "id": "a",
                "adopt": [{"resource_id": "group:gone", "key_id": 7, "account_id": 9}],
            },
            snapshot,
            {9: account},
            state_resources,
        )
        entry = state_resources["a/group:gone"]
        self.assertEqual(entry["status"], "bootstrap_inactive")  # type: ignore[index]
        self.assertEqual(entry["target_account_id"], 9)  # type: ignore[index]

    def test_inactive_adoption_without_marker_fails_before_seeding_state(self) -> None:
        snapshot = ProviderSnapshot(
            "a",
            [],
            [UpstreamKey("7", "personal", "gone", "active", "sk-old")],
        )
        state_resources: dict[str, object] = {}
        account = {"id": 9, "platform": "openai", "type": "apikey", "priority": 3}
        with self.assertRaisesRegex(ReconcileError, "has no ownership marker"):
            _seed_inactive_adoptions(
                {
                    "id": "a",
                    "adopt": [
                        {"resource_id": "group:gone", "key_id": 7, "account_id": 9}
                    ],
                },
                snapshot,
                {9: account},
                state_resources,
            )
        self.assertEqual(state_resources, {})

    def test_delete_is_explicitly_planned_after_grace_and_confirmations(self) -> None:
        marker = marker_for("a", "group:gone")
        key = UpstreamKey("7", marker, "gone", "active", "sk-old")
        inventory = Inventory(
            providers={"a": mock.Mock()},
            snapshots={"a": ProviderSnapshot("a", [], [key])},
            target=mock.Mock(),
            target_accounts=[],
            bindings=[],
            settings={},
        )
        state = {
            "resources": {
                "a/group:gone": {
                    "provider_id": "a",
                    "resource_id": "group:gone",
                    "marker": marker,
                    "upstream_key_id": "7",
                    "missing_count": 2,
                    "missing_since": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
                }
            }
        }
        actions = _action_plan(
            inventory,
            state,
            {
                "delete_upstream_keys": True,
                "delete_grace_hours": 24,
                "delete_min_confirmations": 3,
            },
        )
        self.assertEqual(
            [item.kind for item in actions],
            ["quarantine_missing_resource", "delete_upstream_key"],
        )

    def test_absent_upstream_key_is_tombstoned_without_redelete(self) -> None:
        marker = marker_for("a", "group:gone")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot("a", [], [])
        inventory = Inventory(
            providers={"a": provider},
            snapshots={"a": ProviderSnapshot("a", [], [])},
            target=mock.Mock(),
            target_accounts=[{"id": 9, "schedulable": False}],
            bindings=[],
            settings={},
        )
        state = {
            "resources": {
                "a/group:gone": {
                    "provider_id": "a",
                    "resource_id": "group:gone",
                    "marker": marker,
                    "target_account_id": 9,
                    "upstream_key_id": "7",
                    "missing_count": 3,
                    "missing_since": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
                    "status": "quarantined",
                }
            }
        }
        _apply_missing_resources(
            {
                "delete_upstream_keys": True,
                "delete_grace_hours": 24,
                "delete_min_confirmations": 3,
            },
            inventory,
            state,
            increment=False,
            allow_delete=True,
            planned_deletions={"a/group:gone": "confirm_upstream_key_absent"},
        )
        provider.delete_key.assert_not_called()
        entry = state["resources"]["a/group:gone"]
        self.assertEqual(entry["status"], "upstream_key_deleted_target_retained")
        self.assertIn("upstream_key_deleted_at", entry)

    def test_confirmation_plan_refuses_to_delete_a_reappeared_key(self) -> None:
        marker = marker_for("a", "group:gone")
        key = UpstreamKey("7", marker, "gone", "active", "sk-old")
        provider = mock.Mock()
        provider.scan.return_value = ProviderSnapshot("a", [], [key])
        inventory = Inventory(
            providers={"a": provider},
            snapshots={"a": ProviderSnapshot("a", [], [])},
            target=mock.Mock(),
            target_accounts=[{"id": 9, "schedulable": False}],
            bindings=[],
            settings={},
        )
        state = {
            "resources": {
                "a/group:gone": {
                    "provider_id": "a",
                    "resource_id": "group:gone",
                    "marker": marker,
                    "target_account_id": 9,
                    "upstream_key_id": "7",
                    "missing_count": 3,
                    "missing_since": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
                    "status": "quarantined",
                }
            }
        }
        with self.assertRaisesRegex(ReconcileError, "reappeared after planning"):
            _apply_missing_resources(
                {
                    "delete_upstream_keys": True,
                    "delete_grace_hours": 24,
                    "delete_min_confirmations": 3,
                },
                inventory,
                state,
                increment=False,
                allow_delete=True,
                planned_deletions={
                    "a/group:gone": "confirm_upstream_key_absent"
                },
            )
        provider.delete_key.assert_not_called()


class ConfigAndStoreTests(unittest.TestCase):
    def test_config_validation(self) -> None:
        config = {
            "version": 1,
            "target": {"api_base": "http://127.0.0.1/api/v1", "dashboard_origin": "http://127.0.0.1", "group_id": 9},
            "providers": [
                {
                    "id": "p1",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "include_group_regex": "^gpt",
                    "subscription_resource_allowlist": [],
                    "adopt": [{"resource_id": "group:gpt", "key_id": 1, "account_id": 2}],
                }
            ],
        }
        self.assertIs(validate_config(config), config)

    def test_config_cannot_disable_mandatory_new_group_probes(self) -> None:
        config = {
            "version": 1,
            "target": {
                "api_base": "http://127.0.0.1/api/v1",
                "dashboard_origin": "http://127.0.0.1",
                "group_id": 9,
            },
            "providers": [
                {
                    "id": "p1",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "include_group_regex": "^gpt",
                    "subscription_resource_allowlist": [],
                    "probe_new_resources": False,
                    "adopt": [],
                }
            ],
        }
        with self.assertRaisesRegex(ReconcileError, "cannot disable"):
            validate_config(config)

    def test_historical_revalidation_list_is_explicit_and_canonical(self) -> None:
        config = {
            "version": 1,
            "target": {
                "api_base": "http://127.0.0.1/api/v1",
                "dashboard_origin": "http://127.0.0.1",
                "group_id": 9,
            },
            "providers": [
                {
                    "id": "p1",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "include_group_regex": "^gpt",
                    "subscription_resource_allowlist": [],
                    "probe_revalidate_resource_ids": ["group:gpt"],
                    "adopt": [],
                }
            ],
        }
        self.assertIs(validate_config(config), config)
        config["providers"][0]["probe_revalidate_resource_ids"] = ["gpt"]
        with self.assertRaisesRegex(ReconcileError, "group"):
            validate_config(config)

    def test_target_concurrency_overrides_are_positive_integers(self) -> None:
        config = {
            "version": 1,
            "target": {
                "api_base": "http://127.0.0.1/api/v1",
                "dashboard_origin": "http://127.0.0.1",
                "group_id": 9,
                "concurrency": 100,
            },
            "providers": [
                {
                    "id": "p1",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "include_group_regex": "^gpt",
                    "subscription_resource_allowlist": [],
                    "target_concurrency": 50,
                    "adopt": [],
                }
            ],
        }
        self.assertIs(validate_config(config), config)
        config["providers"][0]["target_concurrency"] = 0
        with self.assertRaisesRegex(ReconcileError, "target_concurrency"):
            validate_config(config)

    def test_subscription_allowlist_is_canonical_and_newapi_fails_closed(self) -> None:
        base = {
            "version": 1,
            "target": {
                "api_base": "http://127.0.0.1/api/v1",
                "dashboard_origin": "http://127.0.0.1",
                "group_id": 9,
            },
            "providers": [
                {
                    "id": "p1",
                    "type": "sub2api",
                    "api_base": "https://example.test/api/v1",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "subscription_resource_allowlist": ["bad"],
                    "adopt": [],
                }
            ],
        }
        base["providers"][0].pop("subscription_resource_allowlist")
        with self.assertRaisesRegex(ReconcileError, "is required"):
            validate_config(base)
        base["providers"][0]["subscription_resource_allowlist"] = ["bad"]
        with self.assertRaisesRegex(ReconcileError, "group"):
            validate_config(base)
        base["providers"][0]["subscription_resource_allowlist"] = ["group:1"]
        with self.assertRaisesRegex(ReconcileError, "require_subscription_expiry"):
            validate_config(base)
        base["providers"][0] = {
            "id": "p1",
            "type": "new-api",
            "api_base": "https://example.test",
            "dashboard_origin": "https://example.test",
            "inference_base": "https://example.test/v1",
            "include_group_regex": "^gpt",
            "subscription_resource_allowlist": ["group:gpt-monthly"],
            "adopt": [],
        }
        with self.assertRaisesRegex(ReconcileError, "authoritative"):
            validate_config(base)

    def test_unsafe_delete_thresholds_are_rejected(self) -> None:
        config = {
            "version": 1,
            "delete_grace_hours": 0,
            "delete_min_confirmations": 1,
            "target": {
                "api_base": "http://127.0.0.1/api/v1",
                "dashboard_origin": "http://127.0.0.1",
                "group_id": 9,
            },
            "providers": [
                {
                    "id": "p1",
                    "type": "new-api",
                    "api_base": "https://example.test",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "include_group_regex": "^gpt",
                    "adopt": [],
                }
            ],
        }
        with self.assertRaisesRegex(ReconcileError, "delete_min_confirmations"):
            validate_config(config)

    def test_missing_provider_credential_keeps_provider_context(self) -> None:
        with mock.patch(
            "upstream_reconciler.store._keychain_find", return_value=(44, None, "missing")
        ):
            with self.assertRaises(ReconcileError) as raised:
                keychain_get("provider:code-plan:password")
        self.assertEqual(raised.exception.code, "credential_missing")
        self.assertEqual(raised.exception.context, {"provider_id": "code-plan"})

    def test_browser_credentials_cannot_cross_configured_origins(self) -> None:
        config = {
            "version": 1,
            "target": {
                "api_base": "http://127.0.0.1/api/v1",
                "dashboard_origin": "http://127.0.0.1",
                "group_id": 9,
            },
            "providers": [
                {
                    "id": "p1",
                    "type": "new-api",
                    "api_base": "https://evil.example",
                    "dashboard_origin": "https://example.test",
                    "inference_base": "https://example.test/v1",
                    "include_group_regex": "^gpt",
                    "adopt": [],
                }
            ],
        }
        with self.assertRaisesRegex(ReconcileError, "same trusted origin"):
            validate_config(config)

    def test_atomic_state_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            atomic_write_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)

    def test_decimal_text_is_canonical(self) -> None:
        self.assertEqual(decimal_text(Decimal("0.0500")), "0.05")


if __name__ == "__main__":
    unittest.main()
