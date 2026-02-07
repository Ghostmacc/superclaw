#!/usr/bin/env python3
"""
Hermes Bridge — Bidirectional Communication Bridge
Port 8787 | The nervous system of the agent network

Mediates all cross-system communication:
  - Agent → Claude Code (via CLI subprocess)
  - Claude Code → Agent (via superclaw CLI subprocess)
  - Any caller → n8n workflows (via webhook trigger)
  - n8n → bridge callback (webhook receiver)

Any CLI tool that can make HTTP requests (Claude Code, Codex CLI, Gemini CLI,
custom scripts) can participate in the agent network through Hermes.

Separate from Lazarus Bridge (state persistence) — different failure domains.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POSTGRES_DSN = os.getenv("POSTGRES_DSN")
if not POSTGRES_DSN:
    print("!! [HERMES] POSTGRES_DSN not set. Run setup.py or copy .env.example to .env")
    print("   Example: export POSTGRES_DSN='postgresql://superclaw:yourpass@localhost:5432/superclaw'")
    exit(1)
N8N_BASE = os.getenv("N8N_BASE_URL", "http://localhost:5678")
POLICY_PATH = os.getenv(
    "HERMES_POLICY",
    os.path.join(os.path.dirname(__file__), "hermes_policy.json"),
)
QUIET_TZ = os.getenv("QUIET_TZ", "America/Chicago")

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
SUPERCLAW_BIN = os.getenv("SUPERCLAW_BIN", "openclaw")

# Timeouts (seconds)
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "120"))
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HERMES] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("hermes")

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
_policy: dict = {}


def load_policy() -> dict:
    global _policy
    try:
        with open(POLICY_PATH) as f:
            _policy = json.load(f)
        log.info("Policy loaded from %s", POLICY_PATH)
    except Exception as e:
        log.error("Failed to load policy: %s — using empty defaults", e)
        _policy = {}
    return _policy


def get_agent_limits(caller_id: str) -> dict:
    per_agent = _policy.get("per_agent", {})
    return per_agent.get(caller_id, {
        "calls_per_hour": 5,
        "max_cost_per_call_usd": 0.10,
        "allowed_targets": [],
        "priority_levels": ["low", "normal"],
    })

# ---------------------------------------------------------------------------
# Rate limiter (in-memory, backed by Postgres counters)
# ---------------------------------------------------------------------------
_rate_counts: dict[str, list[float]] = defaultdict(list)


def _prune(caller_id: str):
    """Remove timestamps older than 1 hour."""
    cutoff = time.time() - 3600
    _rate_counts[caller_id] = [t for t in _rate_counts[caller_id] if t > cutoff]


def check_rate_limit(caller_id: str) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    _prune(caller_id)

    # Global limit
    global_limit = _policy.get("global_limits", {}).get("calls_per_hour", 60)
    total_calls = sum(len(v) for v in _rate_counts.values())
    if total_calls >= global_limit:
        return False, f"Global rate limit ({global_limit}/hr) exceeded"

    # Per-agent limit
    agent_limits = get_agent_limits(caller_id)
    agent_limit = agent_limits.get("calls_per_hour", 5)
    if len(_rate_counts[caller_id]) >= agent_limit:
        return False, f"Agent '{caller_id}' rate limit ({agent_limit}/hr) exceeded"

    return True, "ok"


def record_call(caller_id: str):
    _rate_counts[caller_id].append(time.time())


# ---------------------------------------------------------------------------
# Quiet hours check
# ---------------------------------------------------------------------------
def is_quiet_hours() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(QUIET_TZ))
    except Exception:
        # zoneinfo unavailable — fall back to UTC (quiet hours will be UTC-based)
        log.warning("zoneinfo not available; quiet hours using UTC (set QUIET_TZ or install tzdata)")
        now = datetime.now(timezone.utc)
    hour = now.hour
    qh = _policy.get("quiet_hours", {})
    start = int(qh.get("start", "23:00").split(":")[0])
    end = int(qh.get("end", "08:00").split(":")[0])
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end

# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------
_pg_conn = None


def get_pg():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        try:
            _pg_conn = psycopg2.connect(POSTGRES_DSN)
            _pg_conn.autocommit = False
            log.info("Postgres connected")
        except Exception as e:
            log.error("Postgres connection failed: %s", e)
            _pg_conn = None
            return None
    else:
        try:
            _pg_conn.cursor().execute("SELECT 1")
        except Exception:
            log.warning("Postgres stale, reconnecting")
            try:
                _pg_conn.close()
            except Exception:
                pass
            _pg_conn = None
            return get_pg()
    return _pg_conn


def init_pg_tables():
    conn = get_pg()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hermes_sessions (
                    id TEXT PRIMARY KEY,
                    caller_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    session_type TEXT NOT NULL,
                    claude_session_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_used TIMESTAMPTZ DEFAULT NOW(),
                    message_count INTEGER DEFAULT 0,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hermes_audit_log (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    caller_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    target TEXT,
                    priority TEXT DEFAULT 'normal',
                    request_summary TEXT,
                    response_summary TEXT,
                    latency_ms REAL,
                    cost_usd REAL DEFAULT 0.0,
                    success BOOLEAN,
                    error TEXT,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_hermes_audit_caller
                ON hermes_audit_log (caller_id, timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_hermes_audit_ts
                ON hermes_audit_log (timestamp)
            """)
            # Event outbox for n8n webhooks
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hermes_event_outbox (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload JSONB DEFAULT '{}',
                    webhook_url TEXT,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    delivered_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                ON hermes_event_outbox (status) WHERE status = 'pending'
            """)
            conn.commit()
            log.info("Postgres tables ready")
    except Exception as e:
        conn.rollback()
        log.error("Table init failed: %s", e)


def audit_log(
    caller_id: str,
    endpoint: str,
    target: str = None,
    priority: str = "normal",
    request_summary: str = None,
    response_summary: str = None,
    latency_ms: float = None,
    cost_usd: float = 0.0,
    success: bool = True,
    error: str = None,
    metadata: dict = None,
):
    """Write to Postgres audit log + JSONL backup."""
    conn = get_pg()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO hermes_audit_log
                    (caller_id, endpoint, target, priority, request_summary,
                     response_summary, latency_ms, cost_usd, success, error, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    caller_id, endpoint, target, priority,
                    request_summary[:500] if request_summary else None,
                    response_summary[:500] if response_summary else None,
                    latency_ms, cost_usd, success, error,
                    json.dumps(metadata or {}),
                ))
                conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.error("Audit log Postgres write failed: %s", e)

    # JSONL backup
    if _policy.get("audit", {}).get("log_to_jsonl", True):
        jsonl_path = os.path.expanduser(
            _policy.get("audit", {}).get(
                "jsonl_path",
                "~/.superclaw/workspace/memory/hermes_audit.jsonl",
            )
        )
        try:
            os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "caller_id": caller_id,
                "endpoint": endpoint,
                "target": target,
                "priority": priority,
                "request_summary": request_summary[:200] if request_summary else None,
                "response_summary": response_summary[:200] if response_summary else None,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
                "success": success,
                "error": error,
            }
            with open(jsonl_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.error("JSONL audit write failed: %s", e)


# ---------------------------------------------------------------------------
# Event outbox (Postgres-backed, non-blocking)
# ---------------------------------------------------------------------------
def emit_event(event_type: str, source: str, payload: dict = None):
    """Write event to Postgres outbox. Non-blocking — drain worker delivers to n8n."""
    conn = get_pg()
    if not conn:
        log.warning("Cannot emit event (Postgres down): %s", event_type)
        return
    # Map event_type to webhook URL via policy
    webhooks = _policy.get("event_webhooks", {})
    webhook_path = webhooks.get(event_type, webhooks.get("_default", "/webhook/hermes-events"))
    webhook_url = f"{N8N_BASE}{webhook_path}"
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hermes_event_outbox (event_type, source, payload, webhook_url)
                VALUES (%s, %s, %s, %s)
            """, (event_type, source, json.dumps(payload or {}), webhook_url))
            conn.commit()
        log.debug("Event queued: %s from %s", event_type, source)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("Event emit failed: %s", e)


async def _drain_outbox():
    """Background task: deliver pending events from outbox to n8n webhooks."""
    MAX_ATTEMPTS = 5
    BATCH_SIZE = 20
    POLL_INTERVAL = 5  # seconds

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        conn = get_pg()
        if not conn:
            continue

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, event_type, source, payload, webhook_url, attempts
                    FROM hermes_event_outbox
                    WHERE status = 'pending' AND attempts < %s
                    ORDER BY created_at
                    LIMIT %s
                """, (MAX_ATTEMPTS, BATCH_SIZE))
                rows = cur.fetchall()

            if not rows:
                continue

            async with httpx.AsyncClient(timeout=10.0) as client:
                for row in rows:
                    event_payload = {
                        "event_type": row["event_type"],
                        "source": row["source"],
                        "payload": row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    try:
                        resp = await client.post(row["webhook_url"], json=event_payload)
                        if resp.status_code < 400:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE hermes_event_outbox
                                    SET status = 'delivered', delivered_at = NOW()
                                    WHERE id = %s
                                """, (row["id"],))
                                conn.commit()
                            log.debug("Event %d delivered: %s", row["id"], row["event_type"])
                        else:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE hermes_event_outbox
                                    SET attempts = attempts + 1, last_error = %s
                                    WHERE id = %s
                                """, (f"HTTP {resp.status_code}", row["id"]))
                                conn.commit()
                    except httpx.ConnectError:
                        log.warning("n8n unreachable — events will retry next cycle")
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE hermes_event_outbox
                                SET attempts = attempts + 1, last_error = 'n8n unreachable'
                                WHERE id = %s
                            """, (row["id"],))
                            conn.commit()
                        break  # circuit-break — don't try remaining events
                    except Exception as e:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE hermes_event_outbox
                                SET attempts = attempts + 1, last_error = %s
                                WHERE id = %s
                            """, (str(e)[:500], row["id"]))
                            conn.commit()

                # Mark events that exceeded max attempts as failed
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE hermes_event_outbox
                        SET status = 'failed'
                        WHERE status = 'pending' AND attempts >= %s
                    """, (MAX_ATTEMPTS,))
                    conn.commit()

        except Exception as e:
            log.error("Outbox drain error: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
def get_or_create_session(
    caller_id: str, target: str, session_type: str, purpose: str = "general"
) -> str:
    """Get existing session or create a new one. Returns session ID."""
    session_id = f"hermes-{caller_id}-{target}-{purpose}"
    conn = get_pg()
    if not conn:
        return session_id

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hermes_sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE hermes_sessions
                    SET last_used = NOW(), message_count = message_count + 1
                    WHERE id = %s
                """, (session_id,))
            else:
                claude_sid = str(uuid.uuid4()) if session_type == "claude" else None
                cur.execute("""
                    INSERT INTO hermes_sessions (id, caller_id, target, session_type, claude_session_id)
                    VALUES (%s, %s, %s, %s, %s)
                """, (session_id, caller_id, target, session_type, claude_sid))
            conn.commit()

            cur.execute(
                "SELECT claude_session_id FROM hermes_sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            return session_id
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("Session management error: %s", e)
        return session_id


def get_claude_session_id(hermes_session_id: str) -> Optional[str]:
    conn = get_pg()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT claude_session_id FROM hermes_sessions WHERE id = %s",
                (hermes_session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Subprocess runners
# ---------------------------------------------------------------------------
async def invoke_claude(message: str, session_id: str, resume: bool = False) -> dict:
    """
    Invoke Claude Code CLI as subprocess.
    Returns {"response": str, "cost_usd": float, "session_id": str}.
    """
    claude_sid = get_claude_session_id(session_id)
    if not claude_sid:
        claude_sid = str(uuid.uuid4())
        conn = get_pg()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE hermes_sessions SET claude_session_id = %s WHERE id = %s",
                        (claude_sid, session_id),
                    )
                    conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--session-id", claude_sid,
    ]
    if resume:
        cmd.append("--resume")
    else:
        cmd.append(message)

    log.info("Invoking Claude CLI: session=%s resume=%s", claude_sid, resume)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "hermes-bridge"},
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )

        if proc.returncode != 0:
            err_msg = stderr.decode().strip()[:500]
            log.error("Claude CLI failed (rc=%d): %s", proc.returncode, err_msg)
            return {"response": None, "error": err_msg, "cost_usd": 0.0, "session_id": claude_sid}

        raw = stdout.decode().strip()
        # CLI may print non-JSON lines before the JSON object
        json_str = raw
        json_start = raw.find("\n{")
        if json_start >= 0:
            json_str = raw[json_start + 1:]
        elif raw.startswith("{"):
            json_str = raw

        try:
            data = json.loads(json_str)
            result = data.get("result", json_str)
            # If result is a nested object, extract text content only
            if isinstance(result, dict):
                payloads = result.get("payloads", [])
                texts = [p["text"] for p in payloads if isinstance(p, dict) and p.get("text")]
                response_text = "\n".join(texts) if texts else str(result.get("text", result.get("message", "No response")))
            else:
                response_text = str(result)
            cost = data.get("cost_usd", 0.0)
        except (json.JSONDecodeError, KeyError):
            response_text = raw[:2000]
            cost = 0.0

        return {
            "response": response_text,
            "cost_usd": cost,
            "session_id": claude_sid,
        }

    except asyncio.TimeoutError:
        log.error("Claude CLI timed out after %ds", CLAUDE_TIMEOUT)
        return {"response": None, "error": f"Timeout after {CLAUDE_TIMEOUT}s", "cost_usd": 0.0, "session_id": claude_sid}
    except Exception as e:
        log.error("Claude CLI exception: %s", e)
        return {"response": None, "error": str(e), "cost_usd": 0.0, "session_id": claude_sid}


async def invoke_agent(
    target_agent: str, message: str, session_id: str
) -> dict:
    """
    Invoke SuperClaw agent via CLI subprocess.
    Returns {"response": str, "session_id": str}.
    """
    cmd = [
        SUPERCLAW_BIN, "agent",
        "--agent", target_agent,
        "--message", message,
        "--session-id", session_id,
        "--json",
    ]

    log.info("Invoking SuperClaw agent '%s': session=%s", target_agent, session_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=AGENT_TIMEOUT
        )

        if proc.returncode != 0:
            err_msg = stderr.decode().strip()[:500]
            log.error("SuperClaw agent failed (rc=%d): %s", proc.returncode, err_msg)
            return {"response": None, "error": err_msg}

        raw = stdout.decode().strip()

        # SuperClaw may print plugin lines before JSON — find the JSON object
        json_str = raw
        json_start = raw.find("\n{")
        if json_start >= 0:
            json_str = raw[json_start + 1:]
        elif raw.startswith("{"):
            json_str = raw

        try:
            data = json.loads(json_str)
            # Extract text from SuperClaw --json format, stripping meta/system info
            if "result" in data and isinstance(data["result"], dict):
                payloads = data["result"].get("payloads", [])
                texts = [p["text"] for p in payloads if p.get("text")]
                response_text = "\n".join(texts) if texts else data.get("summary", "No response text")
            else:
                response_text = data.get("response", data.get("output", data.get("text", "No response text")))
        except (json.JSONDecodeError, KeyError):
            # Last resort: strip any obvious JSON blobs from output
            lines = raw.split("\n")
            text_lines = [l for l in lines if not l.strip().startswith(("{", "}", '"')) and not l.strip().startswith("[plugins]")]
            response_text = "\n".join(text_lines).strip() or raw[:500]

        return {"response": response_text}

    except asyncio.TimeoutError:
        log.error("SuperClaw agent timed out after %ds", AGENT_TIMEOUT)
        return {"response": None, "error": f"Timeout after {AGENT_TIMEOUT}s"}
    except Exception as e:
        log.error("SuperClaw agent exception: %s", e)
        return {"response": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------
class ClaudeAskRequest(BaseModel):
    caller_id: str = Field(..., description="Agent ID making the request")
    message: str = Field(..., description="Message to send to Claude Code")
    priority: str = Field("normal", description="low|normal|high|critical")
    purpose: str = Field("general", description="Session purpose tag")
    resume: bool = Field(False, description="Resume existing session instead of new message")
    max_cost_usd: Optional[float] = Field(None, description="Max cost cap for this call")


class AgentAskRequest(BaseModel):
    caller_id: str = Field(..., description="Who is sending")
    target_agent: str = Field(..., description="Target agent name")
    message: str = Field(..., description="Message to send to the agent")
    priority: str = Field("normal", description="low|normal|high|critical")
    purpose: str = Field("general", description="Session purpose tag")


class N8nTriggerRequest(BaseModel):
    caller_id: str = Field(..., description="Who is triggering")
    workflow_path: str = Field(..., description="Webhook path (e.g. '/webhook/my-workflow')")
    payload: dict = Field(default_factory=dict, description="Payload to send to the workflow")
    priority: str = Field("normal", description="low|normal|high|critical")


class N8nWebhookReceiver(BaseModel):
    source_workflow: str = Field(..., description="Which n8n workflow is calling back")
    target: str = Field(..., description="'claude' or agent name")
    message: str = Field(..., description="Message/data to deliver")
    priority: str = Field("normal", description="low|normal|high|critical")
    metadata: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Middleware: policy enforcement
# ---------------------------------------------------------------------------
def enforce_policy(caller_id: str, endpoint: str, priority: str = "normal") -> Optional[str]:
    """
    Check rate limits, quiet hours, and permissions.
    Returns error string if denied, None if allowed.
    """
    # Quiet hours check — exempt callers bypass
    agent_limits = get_agent_limits(caller_id)
    is_exempt = agent_limits.get("quiet_hours_exempt", False) or caller_id == "dashboard"
    if is_quiet_hours() and priority != "critical" and not is_exempt:
        return "Quiet hours active. Only priority='critical' allowed."

    # Rate limit
    allowed, reason = check_rate_limit(caller_id)
    if not allowed:
        return reason

    # Priority level check
    agent_limits = get_agent_limits(caller_id)
    allowed_priorities = agent_limits.get("priority_levels", ["low", "normal"])
    if priority not in allowed_priorities:
        return f"Agent '{caller_id}' not allowed priority '{priority}' (allowed: {allowed_priorities})"

    return None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    import time
    load_policy()
    for attempt in range(10):
        init_pg_tables()
        if get_pg():
            break
        wait = min(3 * (attempt + 1), 10)
        log.warning("Waiting for Postgres (attempt %d/10, retry in %ds)...", attempt + 1, wait)
        time.sleep(wait)
    # Start event outbox drain worker
    drain_task = asyncio.create_task(_drain_outbox())
    log.info("Hermes Bridge v1.1 online — port %s", os.getenv("HERMES_PORT", "8787"))
    yield
    # Shutdown
    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass
    conn = get_pg()
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    log.info("Hermes Bridge shutting down")


app = FastAPI(
    title="Hermes Bridge",
    version="1.1.0",
    description="Bidirectional communication bridge for SuperClaw agent network",
    lifespan=lifespan,
)

# CORS: allow all origins so dashboard, voice bridges, and CLI tools on the LAN
# can reach Hermes without preflight issues. Restrict via HERMES_CORS_ORIGINS if needed.
_cors_origins = os.getenv("HERMES_CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/v1/health")
async def health():
    """Health check — reports all dependency statuses."""
    pg_status = "disconnected"
    conn = get_pg()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM hermes_audit_log"
                )
                count = cur.fetchone()[0]
                pg_status = f"connected ({count} audit entries)"
        except Exception as e:
            pg_status = f"error: {e}"

    claude_ok = False
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        claude_ok = proc.returncode == 0
    except Exception:
        pass

    superclaw_ok = False
    try:
        proc = subprocess.run(
            [SUPERCLAW_BIN, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        superclaw_ok = proc.returncode == 0
    except Exception:
        pass

    n8n_ok = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{N8N_BASE}/healthz", timeout=5.0)
            n8n_ok = resp.status_code < 500
    except Exception:
        pass

    all_ok = pg_status.startswith("connected") and claude_ok and superclaw_ok
    return {
        "status": "healthy" if all_ok else "degraded",
        "version": "1.0.0",
        "postgres": pg_status,
        "claude_cli": "available" if claude_ok else "unavailable",
        "superclaw_cli": "available" if superclaw_ok else "unavailable",
        "n8n": "available" if n8n_ok else "unavailable",
        "policy_loaded": bool(_policy),
        "quiet_hours_active": is_quiet_hours(),
    }


@app.get("/api/v1/policy")
async def get_policy():
    """Return current policy (hot-reloadable)."""
    load_policy()
    return _policy


@app.get("/api/v1/stats")
async def get_stats():
    """Usage statistics — call counts, costs, per-agent breakdowns."""
    conn = get_pg()
    if not conn:
        raise HTTPException(status_code=503, detail="Postgres not connected")

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    caller_id,
                    COUNT(*) as calls,
                    COALESCE(SUM(cost_usd), 0) as total_cost,
                    COUNT(*) FILTER (WHERE success = false) as errors
                FROM hermes_audit_log
                WHERE timestamp > NOW() - INTERVAL '1 hour'
                GROUP BY caller_id
                ORDER BY calls DESC
            """)
            hourly = cur.fetchall()

            cur.execute("""
                SELECT
                    COALESCE(SUM(cost_usd), 0) as total_cost_24h,
                    COUNT(*) as total_calls_24h
                FROM hermes_audit_log
                WHERE timestamp > NOW() - INTERVAL '24 hours'
            """)
            daily = cur.fetchone()

            total_in_memory = sum(len(v) for v in _rate_counts.values())

            return {
                "hourly_by_agent": [dict(r) for r in hourly],
                "daily_totals": dict(daily) if daily else {},
                "in_memory_rate_counter": total_in_memory,
                "global_limit": _policy.get("global_limits", {}).get("calls_per_hour", 60),
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/sessions")
async def list_sessions():
    """List active sessions."""
    conn = get_pg()
    if not conn:
        raise HTTPException(status_code=503, detail="Postgres not connected")

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, caller_id, target, session_type, message_count, last_used
                FROM hermes_sessions
                ORDER BY last_used DESC
                LIMIT 50
            """)
            rows = cur.fetchall()
            return {
                "count": len(rows),
                "sessions": [
                    {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
                    for r in rows
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/claude/ask")
async def claude_ask(req: ClaudeAskRequest):
    """
    Agent → Claude Code.
    Invokes Claude CLI subprocess, returns response.
    """
    start = time.time()

    denied = enforce_policy(req.caller_id, "/claude/ask", req.priority)
    if denied:
        audit_log(
            caller_id=req.caller_id,
            endpoint="/claude/ask",
            target="claude",
            priority=req.priority,
            request_summary=req.message[:200],
            success=False,
            error=f"Policy denied: {denied}",
        )
        raise HTTPException(status_code=429, detail=denied)

    agent_limits = get_agent_limits(req.caller_id)
    max_cost = req.max_cost_usd or agent_limits.get("max_cost_per_call_usd", 0.50)

    record_call(req.caller_id)

    session_id = get_or_create_session(req.caller_id, "claude", "claude", req.purpose)

    result = await invoke_claude(req.message, session_id, resume=req.resume)

    latency = (time.time() - start) * 1000
    cost = result.get("cost_usd", 0.0)
    success = result.get("response") is not None

    audit_log(
        caller_id=req.caller_id,
        endpoint="/claude/ask",
        target="claude",
        priority=req.priority,
        request_summary=req.message[:200],
        response_summary=str(result.get("response", ""))[:200] if success else None,
        latency_ms=latency,
        cost_usd=cost,
        success=success,
        error=result.get("error"),
        metadata={"purpose": req.purpose, "session_id": session_id},
    )

    if not success:
        raise HTTPException(status_code=502, detail=result.get("error", "Claude CLI failed"))

    return {
        "response": result["response"],
        "session_id": session_id,
        "claude_session_id": result.get("session_id"),
        "cost_usd": cost,
        "latency_ms": round(latency, 1),
    }


@app.post("/api/v1/agent/ask")
async def agent_ask(req: AgentAskRequest):
    """
    Claude Code (or agent) → SuperClaw agent.
    Invokes openclaw CLI subprocess, returns response.
    """
    start = time.time()

    denied = enforce_policy(req.caller_id, "/agent/ask", req.priority)
    if denied:
        audit_log(
            caller_id=req.caller_id,
            endpoint="/agent/ask",
            target=req.target_agent,
            priority=req.priority,
            request_summary=req.message[:200],
            success=False,
            error=f"Policy denied: {denied}",
        )
        raise HTTPException(status_code=429, detail=denied)

    record_call(req.caller_id)

    session_id = get_or_create_session(
        req.caller_id, req.target_agent, "agent", req.purpose
    )

    result = await invoke_agent(req.target_agent, req.message, session_id)

    latency = (time.time() - start) * 1000
    success = result.get("response") is not None

    audit_log(
        caller_id=req.caller_id,
        endpoint="/agent/ask",
        target=req.target_agent,
        priority=req.priority,
        request_summary=req.message[:200],
        response_summary=str(result.get("response", ""))[:200] if success else None,
        latency_ms=latency,
        success=success,
        error=result.get("error"),
        metadata={"purpose": req.purpose, "session_id": session_id},
    )

    if not success:
        raise HTTPException(status_code=502, detail=result.get("error", "Agent invocation failed"))

    return {
        "response": result["response"],
        "session_id": session_id,
        "latency_ms": round(latency, 1),
    }


@app.post("/api/v1/n8n/trigger")
async def n8n_trigger(req: N8nTriggerRequest):
    """Trigger an n8n workflow via webhook."""
    start = time.time()

    denied = enforce_policy(req.caller_id, "/n8n/trigger", req.priority)
    if denied:
        audit_log(
            caller_id=req.caller_id,
            endpoint="/n8n/trigger",
            target="n8n",
            priority=req.priority,
            request_summary=f"workflow={req.workflow_path}",
            success=False,
            error=f"Policy denied: {denied}",
        )
        raise HTTPException(status_code=429, detail=denied)

    record_call(req.caller_id)

    url = f"{N8N_BASE}{req.workflow_path}"
    log.info("Triggering n8n workflow: %s (caller=%s)", url, req.caller_id)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=req.payload, timeout=30.0)
            latency = (time.time() - start) * 1000
            success = resp.status_code < 400

            try:
                body = resp.json()
            except Exception:
                body = resp.text

            audit_log(
                caller_id=req.caller_id,
                endpoint="/n8n/trigger",
                target="n8n",
                priority=req.priority,
                request_summary=f"workflow={req.workflow_path}",
                response_summary=str(body)[:200],
                latency_ms=latency,
                success=success,
                error=None if success else f"HTTP {resp.status_code}",
            )

            if not success:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"n8n returned {resp.status_code}: {str(body)[:200]}",
                )

            return {
                "status": "triggered",
                "workflow": req.workflow_path,
                "n8n_response": body,
                "latency_ms": round(latency, 1),
            }

    except httpx.ConnectError:
        audit_log(
            caller_id=req.caller_id,
            endpoint="/n8n/trigger",
            target="n8n",
            request_summary=f"workflow={req.workflow_path}",
            success=False,
            error="n8n connection refused",
        )
        raise HTTPException(status_code=503, detail="n8n is unreachable")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/n8n/webhook-receiver")
async def n8n_webhook_receiver(req: N8nWebhookReceiver):
    """
    n8n calls back into Hermes to reach Claude or an agent.
    Routes the message to the appropriate target.
    """
    start = time.time()
    log.info(
        "n8n webhook callback: workflow=%s target=%s",
        req.source_workflow, req.target,
    )

    if req.target == "claude":
        claude_req = ClaudeAskRequest(
            caller_id=f"n8n:{req.source_workflow}",
            message=req.message,
            priority=req.priority,
            purpose=f"n8n-callback-{req.source_workflow}",
        )
        return await claude_ask(claude_req)
    else:
        agent_req = AgentAskRequest(
            caller_id=f"n8n:{req.source_workflow}",
            target_agent=req.target,
            message=req.message,
            priority=req.priority,
            purpose=f"n8n-callback-{req.source_workflow}",
        )
        return await agent_ask(agent_req)


# ---------------------------------------------------------------------------
# Event outbox endpoints
# ---------------------------------------------------------------------------
class EventSubmitRequest(BaseModel):
    event_type: str = Field(..., description="Event type (e.g. task.created, agent.response)")
    source: str = Field(..., description="Who emitted this event")
    payload: dict = Field(default_factory=dict, description="Event data")


@app.post("/api/v1/events")
async def submit_event(req: EventSubmitRequest):
    """Submit an event to the outbox for delivery to n8n."""
    emit_event(req.event_type, req.source, req.payload)
    return {"status": "queued", "event_type": req.event_type}


@app.get("/api/v1/events/pending")
async def pending_events():
    """List pending events in the outbox."""
    conn = get_pg()
    if not conn:
        raise HTTPException(status_code=503, detail="Postgres not connected")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, event_type, source, status, attempts, last_error,
                       created_at, delivered_at
                FROM hermes_event_outbox
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT 50
            """)
            rows = cur.fetchall()
            return {
                "count": len(rows),
                "events": [
                    {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
                    for r in rows
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/v1/events/delivered")
async def purge_delivered_events():
    """Delete delivered events from the outbox (cleanup)."""
    conn = get_pg()
    if not conn:
        raise HTTPException(status_code=503, detail="Postgres not connected")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hermes_event_outbox WHERE status = 'delivered'")
            deleted = cur.rowcount
            conn.commit()
            return {"deleted": deleted}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Session compaction endpoints
# ---------------------------------------------------------------------------
class CompactRequest(BaseModel):
    agent: str = Field(..., description="Agent to compact (or 'all')")
    threshold: int = Field(150000, description="Token threshold")
    skip_summary: bool = Field(False, description="Skip agent self-summary")
    caller_id: str = Field("dashboard", description="Who triggered this")


@app.post("/api/v1/compact")
async def trigger_compaction(req: CompactRequest):
    """Trigger session compaction for an agent (runs async subprocess)."""
    denied = enforce_policy(req.caller_id, "/compact", "high")
    if denied:
        raise HTTPException(status_code=429, detail=denied)

    record_call(req.caller_id)

    # Find compact_session.py relative to this file
    script = Path(__file__).parent.parent / "scripts" / "compact_session.py"
    if not script.exists():
        raise HTTPException(status_code=404, detail="compact_session.py not found")

    # Build command as list (safe — no shell injection)
    cmd = [sys.executable, str(script)]
    if req.agent == "all":
        cmd.extend(["--all", "--threshold", str(req.threshold)])
    else:
        cmd.extend(["--agent", req.agent, "--threshold", str(req.threshold)])
    if req.skip_summary:
        cmd.append("--skip-summary")

    log.info("Compaction triggered by %s for agent=%s", req.caller_id, req.agent)

    # Note: uses create_subprocess_exec (list-based, no shell) — safe from injection
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    return {
        "status": "started",
        "agent": req.agent,
        "threshold": req.threshold,
        "pid": proc.pid,
    }


@app.get("/api/v1/sessions/sizes")
async def session_sizes():
    """Report token counts for all agent sessions."""
    agents_dir = Path.home() / ".openclaw/agents"
    if not agents_dir.exists():
        return {"agents": {}}

    sizes = {}
    for agent_dir in agents_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        sessions_file = agent_dir / "sessions" / "sessions.json"
        if not sessions_file.exists():
            continue
        try:
            with open(sessions_file) as f:
                data = json.load(f)
            for key, info in data.items():
                if ":main" in key:
                    sizes[agent_dir.name] = {
                        "totalTokens": info.get("totalTokens", 0),
                        "contextTokens": info.get("contextTokens", 262144),
                        "pct": round(
                            info.get("totalTokens", 0) / max(info.get("contextTokens", 262144), 1) * 100, 1
                        ),
                        "sessionId": info.get("sessionId", ""),
                        "model": info.get("model", ""),
                    }
        except Exception:
            continue

    return {"agents": sizes}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("HERMES_PORT", "8787"))
    log.info("Starting Hermes Bridge v1.0")
    log.info("  Postgres: %s", POSTGRES_DSN.split("@")[1] if "@" in POSTGRES_DSN else POSTGRES_DSN)
    log.info("  n8n: %s", N8N_BASE)
    log.info("  Claude CLI: %s", CLAUDE_BIN)
    log.info("  SuperClaw CLI: %s", SUPERCLAW_BIN)
    log.info("  Policy: %s", POLICY_PATH)
    uvicorn.run(app, host="0.0.0.0", port=port)
