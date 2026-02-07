#!/usr/bin/env python3
"""
Lazarus Bridge — Agent State Persistence
Port 8888 | Saves and restores agent state across lifecycles

When an agent session ends (crash, timeout, or completion), it snapshots
its state to Lazarus. When a new instance boots, it queries Lazarus for
the last known state and receives a context injection for continuity.

Persistence layers:
  - Postgres: structured snapshots (queryable, auditable)
  - Qdrant: vector-embedded state (semantic search for related past states)
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
import datetime
import json
import os
import time
import uuid
import httpx

import psycopg2
import psycopg2.extras
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

# ─── Config (all from environment) ────────────────────────────────────────────
POSTGRES_DSN = os.getenv("POSTGRES_DSN")
if not POSTGRES_DSN:
    print("!! [LAZARUS] POSTGRES_DSN not set. Run setup.py or copy .env.example to .env")
    print("   Example: export POSTGRES_DSN='postgresql://superclaw:yourpass@localhost:5432/superclaw'")
    exit(1)
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
COLLECTION_NAME = "lazarus_states"
EMBED_DIMS = 768

# Global clients
qdrant: QdrantClient = None


def get_pg_conn():
    """Get a Postgres connection, reconnecting if needed."""
    if not hasattr(get_pg_conn, "_conn") or get_pg_conn._conn is None or get_pg_conn._conn.closed:
        try:
            get_pg_conn._conn = psycopg2.connect(POSTGRES_DSN)
            get_pg_conn._conn.autocommit = False
            print(">> [LAZARUS] Postgres connected")
        except Exception as e:
            print(f"!! [LAZARUS] Postgres connection failed: {e}")
            get_pg_conn._conn = None
    else:
        try:
            get_pg_conn._conn.cursor().execute("SELECT 1")
        except Exception:
            print(">> [LAZARUS] Postgres connection stale, reconnecting...")
            try:
                get_pg_conn._conn.close()
            except Exception:
                pass
            get_pg_conn._conn = None
            return get_pg_conn()
    return get_pg_conn._conn


def init_qdrant():
    """Initialize Qdrant client and ensure collection exists."""
    global qdrant
    try:
        qdrant = QdrantClient(url=f"http://{QDRANT_HOST}:{QDRANT_PORT}")
        collections = qdrant.get_collections()
        if COLLECTION_NAME not in [c.name for c in collections.collections]:
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=EMBED_DIMS, distance=Distance.COSINE)
            )
            print(f">> [LAZARUS] Qdrant collection '{COLLECTION_NAME}' created")
        else:
            print(f">> [LAZARUS] Qdrant collection '{COLLECTION_NAME}' ready")
    except Exception as e:
        print(f"!! [LAZARUS] Qdrant connection failed: {e}")


def ensure_qdrant():
    """Return Qdrant client, attempting reconnect if stale or None."""
    global qdrant
    if qdrant is not None:
        try:
            qdrant.get_collections()
            return qdrant
        except Exception:
            print(">> [LAZARUS] Qdrant connection lost, reconnecting...")
            qdrant = None
    init_qdrant()
    return qdrant


def init_pg_table():
    """Ensure the snapshots table exists."""
    conn = get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS lazarus_snapshots (
                        id SERIAL PRIMARY KEY,
                        session_id TEXT,
                        agent_id TEXT,
                        agent_name TEXT,
                        status TEXT,
                        last_action TEXT,
                        task_id TEXT,
                        error_log TEXT,
                        next_step_logic TEXT,
                        emotional_state TEXT,
                        timestamp TIMESTAMP,
                        metadata JSONB,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                conn.commit()
                print(">> [LAZARUS] Postgres table ready")
        except Exception as e:
            conn.rollback()
            print(f"!! [LAZARUS] Table init failed: {e}")


def get_embedding(text: str) -> list[float] | None:
    """Get embedding from Ollama."""
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30.0
        )
        resp.raise_for_status()
        embedding = resp.json().get("embedding")
        if embedding and len(embedding) == EMBED_DIMS:
            return embedding
        return None
    except Exception as e:
        print(f"!! [LAZARUS] Embedding failed: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle — retries connections for cold boot."""
    for attempt in range(10):
        init_qdrant()
        init_pg_table()
        conn = get_pg_conn()
        if conn and qdrant:
            break
        wait = min(3 * (attempt + 1), 10)
        services = []
        if not conn:
            services.append("Postgres")
        if not qdrant:
            services.append("Qdrant")
        print(f">> [LAZARUS] Waiting for {', '.join(services)} (attempt {attempt + 1}/10, retry in {wait}s)...")
        time.sleep(wait)
    test_emb = get_embedding("lazarus bridge startup test")
    if test_emb:
        print(f">> [LAZARUS] Embedding model '{EMBED_MODEL}' verified ({len(test_emb)} dims)")
    else:
        print(f"!! [LAZARUS] Embedding model '{EMBED_MODEL}' NOT available — will retry on first use")
    yield
    conn = get_pg_conn()
    if conn:
        try:
            conn.close()
        except Exception:
            pass


app = FastAPI(title="Lazarus Bridge", version="2.0", lifespan=lifespan)


class AgentSnapshot(BaseModel):
    session_id: str
    agent_name: str
    agent_id: str
    status: str  # success, failed, partial, blocked
    last_action: str
    task_id: Optional[str] = None
    error_log: Optional[str] = None
    next_step_logic: str
    emotional_state: Optional[str] = "neutral"
    timestamp: str
    metadata: Optional[Dict] = {}


class ResurrectionResponse(BaseModel):
    context_injection: str
    last_state: Optional[Dict] = None
    continuity_score: float
    semantic_context: Optional[list[str]] = None


@app.post("/snapshot")
async def save_death_state(snapshot: AgentSnapshot):
    """Save agent state on session end."""
    print(f">> [LAZARUS] Snapshot from {snapshot.agent_name} ({snapshot.agent_id})")

    pg_ok = False
    qdrant_ok = False

    conn = get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lazarus_snapshots
                    (session_id, agent_id, agent_name, status, last_action, task_id,
                     error_log, next_step_logic, emotional_state, timestamp, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    snapshot.session_id, snapshot.agent_id, snapshot.agent_name,
                    snapshot.status, snapshot.last_action, snapshot.task_id,
                    snapshot.error_log, snapshot.next_step_logic, snapshot.emotional_state,
                    snapshot.timestamp, json.dumps(snapshot.metadata)
                ))
                conn.commit()
                pg_ok = True
        except Exception as e:
            conn.rollback()
            print(f"   x Postgres error: {e}")

    if ensure_qdrant():
        try:
            embed_text = f"Agent: {snapshot.agent_name}. Status: {snapshot.status}. Action: {snapshot.last_action}. Next: {snapshot.next_step_logic}"
            vector = get_embedding(embed_text)
            if vector:
                # Deterministic ID: re-saving the same agent+session overwrites the previous snapshot
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot.agent_id}:{snapshot.session_id}"))
                qdrant.upsert(
                    collection_name=COLLECTION_NAME,
                    points=[PointStruct(
                        id=point_id,
                        vector=vector,
                        payload={
                            "agent_id": snapshot.agent_id,
                            "agent_name": snapshot.agent_name,
                            "status": snapshot.status,
                            "last_action": snapshot.last_action,
                            "next_step": snapshot.next_step_logic,
                            "task_id": snapshot.task_id,
                            "timestamp": snapshot.timestamp,
                        }
                    )]
                )
                qdrant_ok = True
        except Exception as e:
            print(f"   x Qdrant error: {e}")

    if not pg_ok and not qdrant_ok:
        raise HTTPException(status_code=503, detail="Both stores failed")

    return {
        "status": "ACKNOWLEDGED",
        "agent_id": snapshot.agent_id,
        "persisted_to": {"postgres": pg_ok, "qdrant": qdrant_ok}
    }


@app.get("/resurrect/{agent_id}")
async def get_birth_state(agent_id: str) -> ResurrectionResponse:
    """Restore agent state for a new instance."""
    last_state = None

    conn = get_pg_conn()
    if conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM lazarus_snapshots
                    WHERE agent_id = %s ORDER BY created_at DESC LIMIT 1
                """, (agent_id,))
                row = cur.fetchone()
                if row:
                    last_state = {k: (v.isoformat() if isinstance(v, datetime.datetime) else v) for k, v in dict(row).items()}
        except Exception as e:
            print(f"   x Postgres query error: {e}")

    if not last_state:
        return ResurrectionResponse(
            context_injection="SYSTEM: No previous state found. You are a new instance. Initialize clean.",
            continuity_score=0.0
        )

    # Semantic search for related past states
    semantic_context = []
    if ensure_qdrant() and last_state.get("next_step_logic"):
        try:
            query_vector = get_embedding(f"Agent: {agent_id}. Resuming: {last_state['next_step_logic']}")
            if query_vector:
                results = qdrant.search(
                    collection_name=COLLECTION_NAME,
                    query_vector=query_vector,
                    query_filter={"must": [{"key": "agent_id", "match": {"value": agent_id}}]},
                    limit=3, score_threshold=0.7
                )
                for r in results:
                    p = r.payload
                    semantic_context.append(f"[{p.get('timestamp', '?')}] {p.get('status', '?')}: {p.get('next_step', '?')[:200]}")
        except Exception:
            pass

    # Continuity score (decays over 24h)
    try:
        ts = last_state.get("timestamp") or last_state.get("created_at", "")
        if isinstance(ts, str):
            last_time = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            last_time = ts.replace(tzinfo=datetime.timezone.utc)
        hours_ago = (datetime.datetime.now(datetime.timezone.utc) - last_time).total_seconds() / 3600
        continuity_score = max(0.0, 1.0 - (hours_ago / 24.0))
    except Exception:
        continuity_score = 0.5

    error_section = ""
    if last_state.get("error_log"):
        error_section = f"\nERROR TRACE (DO NOT REPEAT):\n```\n{last_state['error_log'][:500]}\n```\n"

    history_section = ""
    if semantic_context:
        history_section = "\nRELEVANT PAST STATES:\n" + "\n".join(f"- {s}" for s in semantic_context) + "\n"

    injection = f"""*** SYSTEM: LAZARUS PROTOCOL - CONTINUITY MODE ***

You are resuming as {last_state['agent_name']} ({agent_id}).

PREVIOUS SESSION:
- Status: {last_state['status'].upper()}
- Last Action: {last_state['last_action']}
- Task: {last_state.get('task_id', 'No active task')}
- Ended: {last_state.get('timestamp', 'unknown')}
{error_section}
INHERITED DIRECTIVE:
{last_state['next_step_logic']}
{history_section}
CONSTRAINTS:
1. DO NOT repeat the Last Action if it caused an error
2. VALIDATE your approach against the Error Trace
3. If blocked >30 minutes, escalate to Coordinator
4. Log: "Resumed with continuity score {continuity_score:.2f}"
*** END LAZARUS PROTOCOL ***
"""

    return ResurrectionResponse(
        context_injection=injection,
        last_state=last_state,
        continuity_score=continuity_score,
        semantic_context=semantic_context if semantic_context else None
    )


@app.get("/health")
async def health_check():
    """Health check with connection status."""
    pg_status = "disconnected"
    conn = get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM lazarus_snapshots")
                count = cur.fetchone()[0]
                pg_status = f"connected ({count} snapshots)"
        except Exception as e:
            pg_status = f"error: {e}"

    qdrant_status = "disconnected"
    if qdrant:
        try:
            info = qdrant.get_collection(COLLECTION_NAME)
            qdrant_status = f"connected ({info.points_count} points)"
        except Exception as e:
            qdrant_status = f"error: {e}"

    embed_status = "unavailable"
    test = get_embedding("health check")
    if test:
        embed_status = f"ok ({EMBED_MODEL}, {len(test)} dims)"

    return {
        "status": "alive",
        "version": "2.0.0",
        "postgres": pg_status,
        "qdrant": qdrant_status,
        "embeddings": embed_status,
    }


@app.get("/snapshots/{agent_id}")
async def get_snapshots(agent_id: str, limit: int = 10):
    """Get recent snapshots for an agent."""
    conn = get_pg_conn()
    if not conn:
        raise HTTPException(status_code=503, detail="Postgres not connected")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT session_id, status, last_action, timestamp, next_step_logic, created_at
                FROM lazarus_snapshots WHERE agent_id = %s
                ORDER BY created_at DESC LIMIT %s
            """, (agent_id, limit))
            rows = cur.fetchall()
            return {
                "agent_id": agent_id,
                "count": len(rows),
                "snapshots": [
                    {k: (v.isoformat() if isinstance(v, datetime.datetime) else v) for k, v in dict(r).items()}
                    for r in rows
                ]
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    print(">> [LAZARUS] Starting Lazarus Bridge v2.0")
    print(f"   Postgres: {POSTGRES_DSN.split('@')[1] if '@' in POSTGRES_DSN else '(configured)'}")
    print(f"   Qdrant: {QDRANT_HOST}:{QDRANT_PORT}")
    print(f"   Ollama: {OLLAMA_URL} ({EMBED_MODEL})")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("LAZARUS_PORT", "8888")))
