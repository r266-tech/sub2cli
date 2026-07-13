from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from upstream_reconciler.clients import ProviderSnapshot
from upstream_reconciler.core import UpstreamResource, assign_priorities
from upstream_reconciler.runtime import build_inventory


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


class BargainPriorityTests(unittest.TestCase):
    def test_segmented_formula_and_boundary_are_exact(self) -> None:
        items = [
            resource("a", "group:free", "metered", "0"),
            resource("a", "group:bargain", "metered", "0.02"),
            resource("b", "group:bargain-equal", "metered", "0.0200"),
            resource("a", "group:bargain-clamped", "metered", "0.039999"),
            resource("a", "group:subscription", "subscription", None),
            resource("a", "group:boundary", "metered", "0.04"),
            resource("a", "group:over-boundary", "metered", "0.040001"),
            resource("a", "group:standard", "metered", "0.15"),
            resource("a", "group:one", "metered", "1"),
        ]

        result = assign_priorities(items)
        actual = {(item.provider_id, item.resource_id): item.priority for item in result}

        self.assertEqual(actual[("a", "group:free")], 1)
        self.assertEqual(actual[("a", "group:bargain")], 20)
        self.assertEqual(actual[("b", "group:bargain-equal")], 20)
        self.assertEqual(actual[("a", "group:bargain-clamped")], 39)
        self.assertEqual(actual[("a", "group:subscription")], 40)
        self.assertEqual(actual[("a", "group:boundary")], 40)
        self.assertEqual(actual[("a", "group:over-boundary")], 41)
        self.assertEqual(actual[("a", "group:standard")], 150)
        self.assertEqual(actual[("a", "group:one")], 1000)

    def test_new_multiplier_does_not_renumber_existing_routes(self) -> None:
        existing = resource("a", "group:existing", "metered", "0.15")
        assign_priorities([existing])
        self.assertEqual(existing.priority, 150)

        newcomer = resource("b", "group:new", "metered", "0.10")
        assign_priorities([newcomer, existing])
        self.assertEqual(existing.priority, 150)
        self.assertEqual(newcomer.priority, 100)

    def test_subscription_priority_is_one_configurable_fixed_value(self) -> None:
        below = resource("a", "group:below", "metered", "0.029999")
        boundary = resource("a", "group:boundary", "metered", "0.03")
        above = resource("a", "group:above", "metered", "0.030001")
        subscription = resource("a", "group:subscription", "subscription", None)
        assign_priorities(
            [below, boundary, above, subscription], subscription_priority=30
        )
        self.assertEqual(below.priority, 29)
        self.assertEqual(boundary.priority, 30)
        self.assertEqual(subscription.priority, 30)
        self.assertEqual(above.priority, 31)

    def test_only_qualified_input_reserves_priority_tiers(self) -> None:
        bargain = resource("a", "group:failed-bargain", "metered", "0.01")
        subscription = resource("a", "group:subscription", "subscription", None)
        boundary = resource("a", "group:boundary", "metered", "0.04")
        provider = mock.Mock()
        provider.provider_id = "a"
        provider.scan.return_value = ProviderSnapshot(
            "a",
            [bargain, subscription, boundary],
            [],
        )
        target = mock.Mock()
        target.list_accounts.return_value = []

        def qualify(_config, item, _state, *, snapshot=None):
            del snapshot
            if item.resource_id == "group:failed-bargain":
                return False, "probe_failed"
            return True, None

        with (
            mock.patch(
                "upstream_reconciler.runtime.TargetSub2API", return_value=target
            ),
            mock.patch(
                "upstream_reconciler.runtime._validate_target", return_value={}
            ),
            mock.patch(
                "upstream_reconciler.runtime.provider_from_config",
                return_value=provider,
            ),
            mock.patch(
                "upstream_reconciler.runtime._resource_probe_gate",
                side_effect=qualify,
            ),
        ):
            inventory = build_inventory(
                {
                    "target": {},
                    "providers": [{"id": "a", "probe_new_resources": True}],
                },
                {},
            )

        actual = {
            binding.resource.resource_id: binding.resource.priority
            for binding in inventory.bindings
        }

        self.assertEqual(actual["group:subscription"], 40)
        self.assertEqual(actual["group:boundary"], 40)
        self.assertNotIn("group:failed-bargain", actual)
        self.assertEqual(
            inventory.skipped_resources[0]["resource_id"], "group:failed-bargain"
        )


if __name__ == "__main__":
    unittest.main()
