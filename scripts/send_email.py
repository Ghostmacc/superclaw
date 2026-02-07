#!/usr/bin/env python3
"""
Send Email via n8n — SuperClaw Agent Utility

Sends emails through an n8n webhook endpoint. Designed for agent use
via CLI or Python import. Falls back to Docker networking if localhost
is unreachable.

CLI Usage:
  python3 scripts/send_email.py --to user@example.com --subject "Report" --body "Hello"
  python3 scripts/send_email.py --to user@example.com --subject "Report" --body-file report.md --agent monitor
  python3 scripts/send_email.py --test --agent coordinator

Python Usage:
  from send_email import send_email
  ok, msg = send_email("user@example.com", "Subject", "Body text", agent_name="monitor")
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
    os.getenv("N8N_EMAIL_WEBHOOK", "http://localhost:5678/webhook/send-email"),
    "http://n8n:5678/webhook/send-email",
]

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


def send_email(
    to: str,
    subject: str,
    body: str,
    agent_name: str = "unknown",
    cc: str | None = None,
) -> tuple[bool, str]:
    """
    Send an email through n8n webhook.

    Args:
        to:         Recipient email address.
        subject:    Email subject line.
        body:       Email body (plain text or markdown).
        agent_name: Name of the agent sending the email (for audit trail).
        cc:         Optional CC address.

    Returns:
        (success: bool, message: str)
    """
    payload = {
        "to": to,
        "subject": subject,
        "body": body,
        "agent": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if cc:
        payload["cc"] = cc

    last_error = None
    for url in WEBHOOK_URLS:
        try:
            status, resp_body = _post_json(url, payload)
            if 200 <= status < 300:
                return True, f"Email sent successfully via {url} (HTTP {status})"
            else:
                last_error = f"HTTP {status} from {url}: {resp_body}"
        except ConnectionError as e:
            last_error = str(e)
            continue

    return False, f"All endpoints failed. Last error: {last_error}"


def send_test_email(agent_name: str = "test") -> tuple[bool, str]:
    """Send a test email to verify the n8n webhook connection."""
    return send_email(
        to="test@superclaw.local",
        subject=f"[TEST] Email connectivity check from {agent_name}",
        body=(
            f"This is an automated test email sent by agent '{agent_name}' "
            f"at {datetime.now(timezone.utc).isoformat()}.\n\n"
            "If you received this, the n8n email webhook is working correctly."
        ),
        agent_name=agent_name,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send email via n8n webhook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --to user@example.com --subject 'Hello' --body 'World'\n"
            "  %(prog)s --to user@example.com --subject 'Report' --body-file report.md --agent monitor\n"
            "  %(prog)s --test --agent coordinator\n"
        ),
    )
    parser.add_argument("--to", help="Recipient email address")
    parser.add_argument("--subject", help="Email subject line")
    parser.add_argument("--body", help="Email body text")
    parser.add_argument("--body-file", help="Read email body from a file (supports markdown)")
    parser.add_argument("--cc", help="CC email address")
    parser.add_argument("--agent", default="unknown", help="Agent name for audit trail (default: unknown)")
    parser.add_argument("--test", action="store_true", help="Send a test email to verify connectivity")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    # Test mode
    if args.test:
        ok, msg = send_test_email(args.agent)
        if args.json:
            print(json.dumps({"success": ok, "message": msg}))
        else:
            print(f"{'OK' if ok else 'FAIL'}: {msg}")
        return 0 if ok else 1

    # Normal mode — validate required args
    if not args.to:
        parser.error("--to is required (or use --test)")
    if not args.subject:
        parser.error("--subject is required")

    # Resolve body
    if args.body_file:
        body_path = os.path.expanduser(args.body_file)
        if not os.path.isfile(body_path):
            print(f"FAIL: Body file not found: {body_path}", file=sys.stderr)
            return 1
        with open(body_path, "r", encoding="utf-8") as f:
            body = f.read()
    elif args.body:
        body = args.body
    else:
        parser.error("--body or --body-file is required")

    ok, msg = send_email(
        to=args.to,
        subject=args.subject,
        body=body,
        agent_name=args.agent,
        cc=args.cc,
    )

    if args.json:
        print(json.dumps({"success": ok, "message": msg}))
    else:
        print(f"{'OK' if ok else 'FAIL'}: {msg}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
