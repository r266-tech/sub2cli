from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from .core import ReconcileError, redact
from .maintenance import run_maintenance_phase
from .notify import notify_reconcile_error
from .runtime import (
    enroll_from_edge,
    enroll_provider_login,
    reconcile_apply,
    reconcile_plan,
    reconcile_status,
    rollback_snapshot,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sub2cli-reconcile-upstreams",
        description="Reconcile managed upstream keys into Babata Relay priority tiers.",
    )
    parser.add_argument("--config", type=Path, help="private JSON config path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "plan",
        help="deterministic business-state diff (authentication may refresh)",
    )
    subparsers.add_parser("status", help="show last durable local run state")
    subparsers.add_parser("doctor", help="verify auth, provider scans, target scheduler, and plan")
    enroll_parser = subparsers.add_parser(
        "enroll-edge", help="store existing Edge sessions in macOS Keychain"
    )
    enroll_parser.add_argument(
        "--rotate-target-admin-key",
        action="store_true",
        help="intentionally replace the target Admin API Key",
    )
    login_parser = subparsers.add_parser(
        "enroll-login",
        help="validate and store one provider API login in macOS Keychain",
    )
    login_parser.add_argument("--provider", required=True, help="configured provider id")

    apply_parser = subparsers.add_parser("apply", help="apply a fresh plan and verify read-back")
    apply_parser.add_argument("--yes", action="store_true", help="confirm external writes")
    apply_parser.add_argument(
        "--maintenance-safe",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    apply_parser.add_argument(
        "--min-active-resources",
        type=int,
        help=argparse.SUPPRESS,
    )

    rollback_parser = subparsers.add_parser(
        "rollback", help="restore target priority/schedulable fields from a snapshot"
    )
    rollback_parser.add_argument("--snapshot", type=Path, required=True)
    rollback_parser.add_argument("--yes", action="store_true", help="confirm target writes")

    maintenance_parser = subparsers.add_parser(
        "maintenance",
        help="run the bounded schema-repair hard gate",
    )
    maintenance_subparsers = maintenance_parser.add_subparsers(
        dest="maintenance_phase",
        required=True,
    )
    for phase in ("status", "prepare", "verify", "promote"):
        phase_parser = maintenance_subparsers.add_parser(
            phase,
            help=f"{phase} one schema-repair gate",
        )
        if phase in ("verify", "promote"):
            phase_parser.add_argument(
                "--gate-id",
                required=True,
                help="gate id returned by maintenance prepare",
            )
        phase_parser.add_argument(
            "--notify-on-failure",
            action="store_true",
            help="send a sanitized configured Telegram alert when a gate stops",
        )
    return parser


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = reconcile_plan(args.config)
        elif args.command == "status":
            result = reconcile_status()
        elif args.command == "doctor":
            plan = reconcile_plan(args.config)
            result = {"ok": True, "checks": ["config", "keychain", "provider_scan", "target_scheduler", "target_group"], "plan_summary": plan["summary"]}
        elif args.command == "enroll-edge":
            result = enroll_from_edge(
                args.config,
                rotate_target_admin_key=args.rotate_target_admin_key,
            )
        elif args.command == "enroll-login":
            account = getpass.getpass("Account: ")
            password = getpass.getpass("Password: ")
            result = enroll_provider_login(
                args.provider,
                account,
                password,
                args.config,
            )
        elif args.command == "apply":
            if not args.yes:
                raise ReconcileError(
                    "confirmation_required",
                    "apply requires --yes",
                    next_action="review plan, then run apply --yes",
                )
            result = reconcile_apply(
                args.config,
                maintenance_safe=args.maintenance_safe,
                min_active_resources=args.min_active_resources,
            )
        elif args.command == "rollback":
            if not args.yes:
                raise ReconcileError(
                    "confirmation_required",
                    "rollback requires --yes",
                    next_action="review the snapshot, then run rollback --yes",
                )
            result = rollback_snapshot(args.snapshot, args.config)
        elif args.command == "maintenance":
            result = run_maintenance_phase(
                args.maintenance_phase,
                gate_id=getattr(args, "gate_id", None),
                config_path=args.config,
                notify_on_failure=args.notify_on_failure,
            )
        else:  # pragma: no cover
            raise AssertionError(args.command)
        _emit(result)
        return 0
    except ReconcileError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        if exc.next_action:
            payload["error"]["next_action"] = exc.next_action
        if exc.context:
            payload["error"]["context"] = redact(exc.context)
        notification = notify_reconcile_error(exc, config_path=args.config)
        if notification.get("status") not in ("not_applicable", "not_configured"):
            payload["notification"] = notification
        _emit(payload, stream=sys.stderr)
        return 2
    except KeyboardInterrupt:
        _emit(
            {"ok": False, "error": {"code": "interrupted", "message": "operation interrupted"}},
            stream=sys.stderr,
        )
        return 130
    except Exception as exc:  # keep raw exception text out of secret-bearing automation logs
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "unexpected_error",
                    "message": f"unexpected {type(exc).__name__}; inspect the local audit trail",
                },
            },
            stream=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
