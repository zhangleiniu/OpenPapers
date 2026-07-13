"""Explicit P3.S synthetic delivery command; never scheduled or deployed."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from dotenv import load_dotenv

from automation.notification_canary import (
    NotificationCanaryError,
    run_notification_canary,
)
from automation.resend_notifications import (
    ResendNotificationError,
    ResendNotificationTransport,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one isolated synthetic P3.S notification canary."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit one bounded external delivery request",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--approved-recipient-sha256",
        required=True,
        help="SHA-256 of the normalized approved test recipient",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[..., ResendNotificationTransport] = (
        ResendNotificationTransport
    ),
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("notification canary requires explicit --live")
    load_dotenv()
    try:
        run = run_notification_canary(
            output_root=args.output_root,
            approved_recipient_sha256=args.approved_recipient_sha256,
            api_key=os.getenv("RESEND_KEY", ""),
            email_from=os.getenv("OPENPAPERS_CANARY_EMAIL_FROM", ""),
            email_to=os.getenv("OPENPAPERS_CANARY_EMAIL_TO", ""),
            transport_factory=transport_factory,
            now=datetime.now(timezone.utc),
        )
    except (NotificationCanaryError, ResendNotificationError) as exc:
        parser.exit(2, f"notification canary refused: {exc}\n")
    compact = {
        "attempt_count": run.result["delivery"]["attempt_count"],
        "external_request_count": (
            0 if run.replayed else run.result["delivery"]["external_request_count"]
        ),
        "replayed": run.replayed,
        "status": run.result["delivery"]["status"],
        "synthetic_only": True,
    }
    print(json.dumps(compact, sort_keys=True))
    return 0 if run.result["delivery"]["status"] == "delivered" else 3


if __name__ == "__main__":
    raise SystemExit(main())
