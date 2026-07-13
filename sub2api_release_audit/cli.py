from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .runtime import AuditError, run_audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sub2cli-check-sub2api-release",
        description="Read-only official release drift check for the home Sub2API deployment.",
    )
    parser.add_argument("--repo", default="Wei-Shaw/sub2api")
    parser.add_argument("--home-host", default="home")
    parser.add_argument("--home-repo", default="/Users/wzr/code/sub2api")
    parser.add_argument("--container", default="sub2api")
    parser.add_argument("--timeout", type=int, default=15)
    return parser


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.timeout <= 120:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "invalid_timeout",
                    "message": "timeout must be between 1 and 120 seconds",
                },
            },
            stream=sys.stderr,
        )
        return 2
    try:
        _emit(
            run_audit(
                repo=args.repo,
                host=args.home_host,
                home_repo=args.home_repo,
                container=args.container,
                timeout=args.timeout,
            )
        )
        return 0
    except AuditError as exc:
        _emit(
            {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
            stream=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        _emit(
            {"ok": False, "error": {"code": "interrupted", "message": "audit interrupted"}},
            stream=sys.stderr,
        )
        return 130
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "unexpected_error",
                    "message": f"unexpected {type(exc).__name__}",
                },
            },
            stream=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
