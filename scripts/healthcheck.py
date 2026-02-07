#!/usr/bin/env python3
"""
SuperClaw Health Check

Checks all critical services and reports status.
Can run as one-shot or continuous monitor.

Usage:
  python3 scripts/healthcheck.py              # one-shot check
  python3 scripts/healthcheck.py --watch 60   # check every 60s
  python3 scripts/healthcheck.py --json       # machine-readable output
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    print("Missing dependency: httpx")
    print("Install it with:  pip install httpx")
    print("Or run from the bridge venv:  bridge/venv/bin/python scripts/healthcheck.py")
    sys.exit(1)


# Service endpoints — override via environment variables
SERVICES = {
    "ollama": {
        "url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
        "check": "/api/tags",
        "critical": True,
    },
    "qdrant": {
        "url": os.getenv("QDRANT_URL", f"http://{os.getenv('QDRANT_HOST', 'localhost')}:{os.getenv('QDRANT_PORT', '6333')}"),
        "check": "/collections",
        "critical": True,
    },
    "lazarus_bridge": {
        "url": os.getenv("LAZARUS_URL", "http://localhost:8888"),
        "check": "/health",
        "critical": True,
    },
    "hermes_bridge": {
        "url": os.getenv("HERMES_URL", "http://localhost:8787"),
        "check": "/api/v1/health",
        "critical": False,
    },
    "n8n": {
        "url": os.getenv("N8N_URL", "http://localhost:5678"),
        "check": "/healthz",
        "critical": False,
    },
    "postgres": {
        "url": None,
        "check": "bridge",
        "critical": True,
    },
}


def check_http(name: str, base_url: str, path: str, timeout: float = 5.0) -> dict:
    """Check an HTTP service."""
    url = f"{base_url}{path}"
    start = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return {
            "name": name,
            "status": "up" if resp.status_code < 500 else "degraded",
            "code": resp.status_code,
            "latency_ms": round(latency_ms, 1),
        }
    except httpx.ConnectError:
        return {"name": name, "status": "down", "error": "connection refused"}
    except httpx.TimeoutException:
        return {"name": name, "status": "down", "error": "timeout"}
    except Exception as e:
        return {"name": name, "status": "down", "error": str(e)}


def check_ollama_models() -> dict:
    """Check which embedding models are loaded in Ollama."""
    try:
        resp = httpx.get(f"{SERVICES['ollama']['url']}/api/tags", timeout=5.0)
        models = [m["name"] for m in resp.json().get("models", [])]
        embed_models = [m for m in models if "embed" in m.lower()]
        return {"loaded_models": len(models), "embed_models": embed_models}
    except Exception:
        return {"loaded_models": 0, "embed_models": []}


def check_qdrant_collections() -> dict:
    """Check Qdrant collections and point counts."""
    try:
        resp = httpx.get(f"{SERVICES['qdrant']['url']}/collections", timeout=5.0)
        data = resp.json().get("result", {}).get("collections", [])
        collections = {}
        for c in data:
            name = c.get("name", "unknown")
            collections[name] = "active"
        return {"collections": collections}
    except Exception as e:
        return {"collections": {}, "error": str(e)}


def check_bridge_deep() -> dict:
    """Get Lazarus Bridge health details including Postgres status."""
    try:
        resp = httpx.get(f"{SERVICES['lazarus_bridge']['url']}/health", timeout=5.0)
        data = resp.json()
        return {
            "postgres": data.get("postgres", "unknown"),
            "qdrant": data.get("qdrant", "unknown"),
            "agent_states": data.get("agent_states_count", 0),
        }
    except Exception:
        return {"postgres": "unknown", "qdrant": "unknown"}


def run_healthcheck(as_json: bool = False) -> dict:
    """Run full health check across all services."""
    timestamp = datetime.now(timezone.utc).isoformat()
    results = []

    for name, svc in SERVICES.items():
        if svc["url"] and svc["check"] != "bridge":
            result = check_http(name, svc["url"], svc["check"])
            result["critical"] = svc["critical"]
            results.append(result)

    ollama_info = check_ollama_models()
    qdrant_info = check_qdrant_collections()
    bridge_info = check_bridge_deep()

    pg_status = bridge_info.get("postgres", "unknown")
    results.append({
        "name": "postgres",
        "status": "up" if "connected" in str(pg_status) else "down",
        "critical": True,
        "via": "lazarus_bridge",
        "detail": pg_status,
    })

    critical_down = [r for r in results if r.get("critical") and r["status"] == "down"]
    overall = "healthy" if not critical_down else "degraded" if len(critical_down) < 2 else "critical"

    report = {
        "timestamp": timestamp,
        "overall": overall,
        "services": results,
        "ollama": ollama_info,
        "qdrant": qdrant_info,
        "bridge": bridge_info,
    }

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)

    return report


def print_report(report: dict):
    """Pretty-print the health report."""
    overall = report["overall"]
    status_icon = {"healthy": "[OK]", "degraded": "[!!]", "critical": "[XX]"}.get(overall, "[??]")

    print(f"\n  SUPERCLAW HEALTH CHECK")
    print(f"  {'='*50}")
    print(f"  Status: {status_icon} {overall.upper()}")
    print(f"  Time:   {report['timestamp'][:19]}Z")
    print(f"  {'='*50}")

    for svc in report["services"]:
        name = svc["name"].ljust(16)
        status = svc["status"]
        icon = {"up": "+", "down": "X", "degraded": "!"}.get(status, "?")
        extra = ""
        if "latency_ms" in svc:
            extra = f" ({svc['latency_ms']}ms)"
        elif "error" in svc:
            extra = f" - {svc['error']}"
        elif "via" in svc:
            extra = f" (via {svc['via']})"
        crit = "*" if svc.get("critical") else " "
        print(f"  [{icon}]{crit} {name} {status}{extra}")

    ollama = report.get("ollama", {})
    if ollama.get("embed_models"):
        print(f"\n  Embed models: {', '.join(ollama['embed_models'])}")

    qdrant = report.get("qdrant", {})
    collections = qdrant.get("collections", {})
    if collections:
        print(f"\n  Qdrant collections:")
        for name in collections:
            print(f"    {name}")

    print(f"\n  * = critical service")
    print()


def main():
    parser = argparse.ArgumentParser(description="SuperClaw Health Check")
    parser.add_argument("--watch", type=int, metavar="SECS", help="Continuous monitoring interval")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.watch:
        print(f"[*] Watching services every {args.watch}s (Ctrl+C to stop)\n")
        try:
            while True:
                run_healthcheck(as_json=args.json)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n[*] Stopped")
    else:
        report = run_healthcheck(as_json=args.json)
        critical_down = [s for s in report["services"] if s.get("critical") and s["status"] == "down"]
        sys.exit(1 if critical_down else 0)


if __name__ == "__main__":
    main()
