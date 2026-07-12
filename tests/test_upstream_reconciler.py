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
    Action,
    Binding,
    ReconcileError,
    UpstreamKey,
    UpstreamResource,
    assign_priorities,
    decimal_text,
    desired_metadata,
    marker_for,
    redact,
)
from upstream_reconciler.runtime import (
    Inventory,
    _action_plan,
    _apply_active_resources,
    _apply_missing_resources,
    _choose_key,
    _choose_target_account,
    _seed_inactive_adoptions,
    validate_config,
)
from upstream_reconciler.store import atomic_write_json


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
        self.assertEqual(actual[("a", "group:sub")], 1)
        self.assertEqual(actual[("a", "group:5")], 2)
        self.assertEqual(actual[("b", "group:5")], 2)
        self.assertEqual(actual[("b", "group:8")], 3)
        self.assertEqual(actual[("a", "group:15")], 4)

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

    def test_newapi_normalizes_management_token_for_inference(self) -> None:
        self.assertEqual(NewAPIProvider._inference_secret("a" * 48), "sk-" + "a" * 48)
        self.assertEqual(NewAPIProvider._inference_secret("sk-abc"), "sk-abc")
        self.assertEqual(NewAPIProvider._inference_secret("abc***xyz"), "abc***xyz")

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


class PlanTests(unittest.TestCase):
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
        snapshot = ProviderSnapshot(
            "a",
            [],
            [UpstreamKey("7", "old", "gone", "active", "sk-old")],
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
                    "adopt": [{"resource_id": "group:gpt", "key_id": 1, "account_id": 2}],
                }
            ],
        }
        self.assertIs(validate_config(config), config)

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
