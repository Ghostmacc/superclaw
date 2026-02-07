#!/usr/bin/env python3
"""
Mission Control Data Sync — reads TASKS.json and agent session data.

Outputs mission-control-data.json for the dashboard to consume.

Usage:
  python3 sync-mission-data.py           # one-shot sync
  python3 sync-mission-data.py --watch   # auto-sync on file changes
"""

import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# Paths — adjust these for your deployment
TASKS_FILE = Path.home() / ".superclaw/workspace/memory/squad/TASKS.json"
OUTPUT_FILE = Path(__file__).parent / "mission-control-data.json"
AGENTS_DIR = Path.home() / ".superclaw/agents"
COLLAB_DIR = Path.home() / "collab"
CCRP_PACKET_FILE = COLLAB_DIR / "context" / "ccrp_packet.latest.json"
WARNING_STREAM_FILE = COLLAB_DIR / "context" / "warnings.jsonl"
THREAD_FILE = COLLAB_DIR / "thread.jsonl"

# Template agent IDs — customize to match your agents/ directory
AGENT_IDS = ["coordinator", "developer", "researcher", "monitor", "analyst"]

AGENT_NAMES = {
    "coordinator": "Coordinator",
    "developer": "Developer",
    "researcher": "Researcher",
    "monitor": "Monitor",
    "analyst": "Analyst",
}

AGENT_ROLES = {
    "coordinator": "Task Routing & Management",
    "developer": "Code & Automation",
    "researcher": "Intelligence & Synthesis",
    "monitor": "Infrastructure Guardian",
    "analyst": "Budget & Cost Analysis",
}

STATE_MAP = {
    "inbox": "inbox",
    "assigned": "assigned",
    "in_progress": "inProgress",
    "review": "review",
    "done": "done",
}


def load_tasks():
    if not TASKS_FILE.exists():
        return {"tasks": [], "agents": [], "activity_log": []}
    with open(TASKS_FILE) as f:
        return json.load(f)


def _tail_lines(path, limit=200):
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(deque(f, maxlen=limit))


def _iso_to_hhmm(ts):
    if "T" in ts:
        return ts.split("T", 1)[1][:5]
    return ""


def _extract_severity(text):
    t = text.lower()
    if "critical" in t:
        return "critical"
    if "high" in t:
        return "high"
    if "low" in t:
        return "low"
    return "medium"


def _parse_warning_text(text):
    msg = text.strip()
    if msg.lower().startswith("warning:"):
        msg = msg.split(":", 1)[1].strip()
    severity = _extract_severity(msg)
    scope = "general"

    # Optional compact format:
    # WARNING|severity=high|scope=hermes|message=Quiet hours policy is active
    parts = [p.strip() for p in msg.split("|")]
    if len(parts) > 1 and any("=" in p for p in parts[1:]):
        kv = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[k.strip().lower()] = v.strip()
        severity = kv.get("severity", severity)
        scope = kv.get("scope", scope)
        msg = kv.get("message", msg)

    return {
        "severity": severity,
        "scope": scope,
        "message": msg,
    }


def load_context_packet():
    if not CCRP_PACKET_FILE.exists():
        return None
    try:
        with open(CCRP_PACKET_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    mission = raw.get("mission_state", {})
    objective = (
        mission.get("current_objective")
        or mission.get("objective")
        or mission.get("summary")
        or ""
    )
    next_actions = raw.get("next_actions", [])
    if not isinstance(next_actions, list):
        next_actions = []

    return {
        "packetId": raw.get("packet_id", ""),
        "createdAt": raw.get("created_at", ""),
        "createdBy": raw.get("created_by", ""),
        "objective": objective,
        "openQuestions": len(raw.get("open_questions", []) or []),
        "nextActions": [str(x) for x in next_actions[:5]],
    }


def load_warnings(limit=20):
    warnings = []
    seen = set()

    if WARNING_STREAM_FILE.exists():
        for line in _tail_lines(WARNING_STREAM_FILE, limit=200):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = str(rec.get("message", "")).strip()
            if not msg:
                continue
            severity = str(rec.get("severity", _extract_severity(msg))).lower()
            scope = str(rec.get("scope", "general"))
            ts = str(rec.get("timestamp", ""))
            src = str(rec.get("source", "anchor"))
            key = (ts, severity, msg)
            if key in seen:
                continue
            seen.add(key)
            warnings.append({
                "timestamp": ts,
                "time": _iso_to_hhmm(ts),
                "severity": severity,
                "scope": scope,
                "source": src,
                "message": msg,
            })

    if THREAD_FILE.exists():
        for line in _tail_lines(THREAD_FILE, limit=400):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sender = str(rec.get("from", "")).lower()
            msg_type = str(rec.get("type", "")).lower()
            content = str(rec.get("content", "")).strip()
            if not content:
                continue
            content_l = content.lower()

            is_anchor_warning = (
                sender == "anchor"
                and (
                    msg_type == "anchor"
                    or "warning" in content_l
                    or content_l.startswith("warn|")
                    or content_l.startswith("warning|")
                )
            )
            if not is_anchor_warning:
                continue

            parsed = _parse_warning_text(content)
            ts = str(rec.get("timestamp", ""))
            key = (ts, parsed["severity"], parsed["message"])
            if key in seen:
                continue
            seen.add(key)
            warnings.append({
                "timestamp": ts,
                "time": _iso_to_hhmm(ts),
                "severity": parsed["severity"],
                "scope": parsed["scope"],
                "source": "anchor",
                "message": parsed["message"],
            })

    warnings.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return warnings[:limit]


def build_context():
    packet = load_context_packet()
    warnings = load_warnings(limit=20)
    return {
        "packet": packet,
        "warnings": warnings,
    }


def build_agents(data):
    agents = []
    for agent in data.get("agents", []):
        aid = agent.get("id", "")
        status = agent.get("status", "idle")
        name = agent.get("name", AGENT_NAMES.get(aid, aid))
        role = agent.get("role", AGENT_ROLES.get(aid, ""))

        current_task = None
        for task in data.get("tasks", []):
            if aid in task.get("assigneeIds", []) and task.get("status") in ("in_progress", "assigned"):
                current_task = task.get("title", "")
                if status == "idle":
                    status = "active"
                break

        agents.append({
            "name": name, "key": aid, "role": role,
            "status": status, "task": current_task, "lastCheckin": "\u2014",
        })
    return agents


def build_tasks(data):
    columns = {"inbox": [], "assigned": [], "inProgress": [], "review": [], "done": []}

    for task in data.get("tasks", []):
        state = task.get("status", "inbox")
        column = STATE_MAP.get(state, "inbox")

        assignee_ids = task.get("assigneeIds", [])
        agent_name = "\u2014"
        if assignee_ids:
            for a in data.get("agents", []):
                if a.get("id") == assignee_ids[0]:
                    agent_name = a.get("name", assignee_ids[0])
                    break
            else:
                agent_name = assignee_ids[0]

        ts = task.get("completedAt") or ""
        if not ts and task.get("comments"):
            ts = task["comments"][-1].get("timestamp", "")
        if not ts:
            ts = task.get("createdAt", "")

        time_str = ts.split("T")[1][:5] if "T" in ts else ""

        comments = []
        for cmt in task.get("comments", []):
            cmt_ts = cmt.get("timestamp", "")
            comments.append({
                "agent": cmt.get("fromAgentId", "system"),
                "text": cmt.get("content", ""),
                "time": cmt_ts.split("T")[1][:5] if "T" in cmt_ts else "",
                "date": cmt_ts.split("T")[0] if "T" in cmt_ts else "",
            })

        columns[column].append({
            "id": task.get("id", ""),
            "title": task.get("title", ""),
            "agent": agent_name,
            "time": time_str,
            "description": task.get("description", ""),
            "priority": task.get("priority", "normal"),
            "tags": task.get("tags", []),
            "comments": comments,
            "commentCount": len(comments),
        })

    return columns


def build_activity(data):
    activity = []
    for entry in data.get("activities", []):
        ts = entry.get("timestamp", "")
        activity.append({
            "agent": entry.get("agentId", "system"),
            "text": entry.get("message", ""),
            "time": ts.split("T")[1][:5] if "T" in ts else "",
            "sort_ts": ts,
        })

    activity.sort(key=lambda x: x.get("sort_ts", ""), reverse=True)
    for a in activity:
        a.pop("sort_ts", None)
    return activity[:20]


def build_token_usage():
    provider_agg = {}
    agent_agg = {}

    for aid in AGENT_IDS:
        sf = AGENTS_DIR / aid / "sessions" / "sessions.json"
        if not sf.exists():
            continue
        try:
            with open(sf) as f:
                sessions = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        a_in = a_out = a_total = a_count = 0
        provider_seen = {}

        for sess in sessions.values():
            inp = sess.get("inputTokens", 0)
            out = sess.get("outputTokens", 0)
            tot = sess.get("totalTokens", 0)
            provider = sess.get("modelProvider", "unknown")
            model = sess.get("model", "unknown")

            a_in += inp
            a_out += out
            a_total += tot
            a_count += 1
            provider_seen[provider] = provider_seen.get(provider, 0) + 1

            key = (provider, model)
            if key not in provider_agg:
                provider_agg[key] = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "sessionCount": 0}
            provider_agg[key]["inputTokens"] += inp
            provider_agg[key]["outputTokens"] += out
            provider_agg[key]["totalTokens"] += tot
            provider_agg[key]["sessionCount"] += 1

        primary = max(provider_seen, key=provider_seen.get) if provider_seen else "unknown"
        agent_agg[aid] = {
            "agentId": aid, "name": AGENT_NAMES.get(aid, aid),
            "inputTokens": a_in, "outputTokens": a_out,
            "totalTokens": a_total, "sessionCount": a_count,
            "primaryProvider": primary,
        }

    by_provider = [
        {"provider": k[0], "model": k[1], **v}
        for k, v in sorted(provider_agg.items(), key=lambda x: x[1]["totalTokens"], reverse=True)
    ]
    by_agent = sorted(agent_agg.values(), key=lambda x: x["totalTokens"], reverse=True)

    return {
        "byProvider": by_provider,
        "byAgent": by_agent,
        "grandTotal": {
            "inputTokens": sum(a["inputTokens"] for a in by_agent),
            "outputTokens": sum(a["outputTokens"] for a in by_agent),
            "totalTokens": sum(a["totalTokens"] for a in by_agent),
            "sessionCount": sum(a["sessionCount"] for a in by_agent),
            "providerCount": len(by_provider),
        },
    }


def sync():
    data = load_tasks()
    now = datetime.now(timezone.utc).astimezone()
    timestamp = now.strftime("%Y-%m-%d %H:%M %Z")

    output = {
        "lastUpdated": timestamp,
        "agents": build_agents(data),
        "tasks": build_tasks(data),
        "activity": build_activity(data),
        "budget": build_token_usage(),
        "context": build_context(),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Synced {len(data.get('tasks',[]))} tasks -> {OUTPUT_FILE.name} [{timestamp}]")


def _get_watched_mtimes():
    mtimes = []
    if TASKS_FILE.exists():
        mtimes.append(TASKS_FILE.stat().st_mtime)
    if CCRP_PACKET_FILE.exists():
        mtimes.append(CCRP_PACKET_FILE.stat().st_mtime)
    if WARNING_STREAM_FILE.exists():
        mtimes.append(WARNING_STREAM_FILE.stat().st_mtime)
    if THREAD_FILE.exists():
        mtimes.append(THREAD_FILE.stat().st_mtime)
    for aid in AGENT_IDS:
        sf = AGENTS_DIR / aid / "sessions" / "sessions.json"
        if sf.exists():
            mtimes.append(sf.stat().st_mtime)
    return max(mtimes) if mtimes else 0


def watch(interval=10):
    print(f"[*] Watching TASKS.json + session files (Ctrl+C to stop)")
    last_mtime = 0
    while True:
        try:
            mtime = _get_watched_mtimes()
            if mtime != last_mtime:
                sync()
                last_mtime = mtime
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[*] Stopped")
            break


if __name__ == "__main__":
    if "--watch" in sys.argv:
        sync()
        watch()
    else:
        sync()
