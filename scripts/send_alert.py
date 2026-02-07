#!/usr/bin/env python3
"""
Send Alert via n8n — SuperClaw Agent Utility

Sends agent alerts (info/warning/critical) through an n8n webhook
endpoint. Designed for agent use via CLI or Python import. Falls back
to Docker networking if localhost is unreachable.

CLI Usage:
  python3 scripts/send_alert.py --agent monitor --severity warning --title "Disk 90%" --details "Partition /dev/sda1 is 90% full"
  python3 scripts/send_alert.py --agent researcher --severity info --title "Research complete" --details "Found 12 relevant papers"
  python3 scripts/send_alert.py --agent analyst --severity critical --title "Budget exceeded" --details "Token spend is 120% of daily limit"

Python Usage:
  from send_alert import send_alert
  ok, msg = send_alert("monitor", "warning", "Disk 90%", "Partition /dev/sda1 is 90% full")
"""

import sys
import os
import json
import argparse
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# n8n webhook endpoints — localhost first, Docker fallback
WEBHOOK_URLS = [
    os.getenv("N8N_ALERT_WEBHOOK", "http://localhost:5678/webhook/agent-alert"),
    "http://n8n:5678/webhook/agent-alert",
]

VALID_SEVERITIES = ("info", "warning", "critical")

TIMEOUT_SECONDS = 15


def _post_json(url: str, payload: dict, timeout: int = TIMEOUT_SECONDS) -> tuple[int, str]:
    """POST JSON to a URL using stdlib. Returns (status_code, response_body)."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        return e.code, body
    except URLError as e:
        raise ConnectionError(f"Cannot reach {url}: {e.reason}") from e


def send_alert(
    agent_name: str,
    severity: str,
    title: str,
    details: str = "",
    tags: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Send an agent alert through n8n webhook.

    Args:
        agent_name: Name of the agent raising the alert.
        severity:   One of "info", "warning", "critical".
        title:      Short alert title.
        details:    Longer description or context (optional).
        tags:       Optional list of tags for filtering/routing.

    Returns:
        (success: bool, message: str)
    """
    severity = severity.lower().strip()
    if severity not in VALID_SEVERITIES:
        return False, f"Invalid severity '{severity}'. Must be one of: {', '.join(VALID_SEVERITIES)}"

    payload = {
        "agent": agent_name,
        "severity": severity,
        "title": title,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if tags:
        payload["tags"] = tags

    last_error = None
    for url in WEBHOOK_URLS:
        try:
            status, resp_body = _post_json(url, payload)
            if 200 <= status < 300:
                return True, f"Alert sent successfully via {url} (HTTP {status})"
            else:
                last_error = f"HTTP {status} from {url}: {resp_body}"
        except ConnectionError as e:
            last_error = str(e)
            continue

    return False, f"All endpoints failed. Last error: {last_error}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send agent alert via n8n webhook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --agent monitor --severity warning --title 'Disk 90%%' --details 'sda1 is 90%% full'\n"
            "  %(prog)s --agent analyst --severity critical --title 'Budget exceeded'\n"
            "  %(prog)s --agent researcher --severity info --title 'Scan done' --tags research,web\n"
        ),
    )
    parser.add_argument("--agent", required=True, help="Agent name raising the alert")
    parser.add_argument(
        "--severity",
        required=True,
        choices=VALID_SEVERITIES,
        help="Alert severity level",
    )
    parser.add_argument("--title", required=True, help="Short alert title")
    parser.add_argument("--details", default="", help="Longer description or context")
    parser.add_argument("--tags", help="Comma-separated tags for filtering (e.g. disk,infra)")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    ok, msg = send_alert(
        agent_name=args.agent,
        severity=args.severity,
        title=args.title,
        details=args.details,
        tags=tags,
    )

    if args.json:
        print(json.dumps({"success": ok, "message": msg}))
    else:
        prefix = "OK" if ok else "FAIL"
        severity_marker = {"info": "[i]", "warning": "[!]", "critical": "[!!!]"}[args.severity]
        print(f"{prefix} {severity_marker}: {msg}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
