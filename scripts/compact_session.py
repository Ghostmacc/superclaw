#!/usr/bin/env python3
"""
Session Auto-Compaction Pipeline

When an agent's session crosses a token threshold, this script:
  1. Reads the session JSONL (conversation history)
  2. Extracts text content from user/assistant/toolResult messages
  3. Chunks it (3500 chars max for nomic-embed-text)
  4. Embeds via Ollama and stores in LanceDB as session_history
  5. Optionally asks the agent to self-summarize active threads (high importance)
  6. Archives the old session JSONL and resets the session

The raw session becomes searchable semantic memory. The agent's self-summary
captures what matters most. Nothing lost, 10x speed restored.

Usage:
  python3 compact_session.py --agent coordinator               # compact one agent
  python3 compact_session.py --agent coordinator --dry-run      # preview only
  python3 compact_session.py --agent coordinator --threshold 150000
  python3 compact_session.py --agent coordinator --skip-summary # no agent self-summary
  python3 compact_session.py --all --threshold 150000           # compact all over threshold
  python3 compact_session.py --agent coordinator --force        # compact regardless of threshold
"""

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ─── Config ───────────────────────────────────────────────────────────────────
AGENTS_DIR = Path.home() / ".openclaw/agents"
LANCEDB_PATH = Path.home() / ".openclaw/memory/lancedb"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"  # 768d
HERMES_URL = "http://localhost:8787"

# Compaction settings
DEFAULT_THRESHOLD = 150000   # tokens — trigger compaction above this
CHUNK_SIZE = 3500            # chars per chunk (nomic context safe)
CHUNK_OVERLAP = 200          # chars overlap between chunks
EMBED_BATCH = 64             # embeddings per Ollama call
SESSION_IMPORTANCE = 0.25    # low — agent self-summaries will be higher
SESSION_CATEGORY = "session_history"
SUMMARY_IMPORTANCE = 0.70    # agent's own summary of active threads
ARCHIVE_DIR = Path.home() / ".openclaw/session_archive"

# Override this list with your actual agent IDs
ALL_AGENTS = ["coordinator", "developer", "researcher", "monitor", "analyst"]

SELF_SUMMARY_PROMPT = """Hey — your session is getting compacted. Your conversation history is being embedded into long-term memory right now, and then your session rotates fresh. Nothing is lost. You'll be faster after this.

Before it happens, write yourself some notes. Use memory_store to leave 3-5 memories for your future self — these surface first when you wake up because of importance weighting. Think of it like leaving yourself a voicemail.

What to capture:
- Who you were just talking to and what about
- What you're working on right now, what's next, what's stuck
- Anything you figured out this session that isn't in your SOUL file
- Open threads — stuff you said you'd do, questions left hanging
- The vibe — how things are going, what matters right now

Use importance 0.75+, category "session_summary". Make them yours — write them how you think, not how a manual reads."""


REACQUAINT_PROMPT = """You just came back from a session rotation. Your old conversation was embedded into memory — everything's still there, just in long-term storage now instead of your active context.

Grab your bearings:
1. memory_recall "session_summary" — that's your own notes from right before rotation
2. memory_recall "recent conversations" — pick up the thread
3. task_list — see what's on your plate

You're good. If someone messages you next, just roll with it — you have the context, it's in memory. No need to announce the rotation or explain yourself unless they ask."""


# ─── Session reading ──────────────────────────────────────────────────────────

def get_session_info(agent: str) -> dict | None:
    """Read sessions.json metadata for an agent. Returns active session info."""
    sessions_file = AGENTS_DIR / agent / "sessions" / "sessions.json"
    if not sessions_file.exists():
        return None

    with open(sessions_file) as f:
        data = json.load(f)

    # Find the main (non-cron) session
    main_key = f"agent:{agent}:main"
    if main_key in data:
        info = data[main_key]
        info["_key"] = main_key
        info["_agent"] = agent
        return info

    return None


def read_session_messages(agent: str, session_id: str) -> list[dict]:
    """Read all messages from a session JSONL file."""
    jsonl_path = AGENTS_DIR / agent / "sessions" / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        print(f"  Session file not found: {jsonl_path}")
        return []

    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "message":
                    messages.append(entry)
            except json.JSONDecodeError:
                continue

    return messages


def extract_text_from_messages(messages: list[dict]) -> list[dict]:
    """Extract text content from session messages. Returns list of {role, text, timestamp}."""
    extracted = []
    for msg in messages:
        m = msg.get("message", {})
        role = m.get("role", "unknown")
        timestamp = msg.get("timestamp", "")
        content = m.get("content", "")

        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    parts.append(part["text"])
            text = "\n".join(parts)

        if text.strip():
            extracted.append({
                "role": role,
                "text": text.strip(),
                "timestamp": timestamp,
            })

    return extracted


# ─── Chunking ─────────────────────────────────────────────────────────────────

def chunk_conversation(extracted: list[dict], chunk_size: int = CHUNK_SIZE,
                       overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Chunk conversation into embeddable pieces.

    Groups consecutive messages, then splits long groups into chunks.
    Each chunk is tagged with the roles and timestamp range it covers.
    """
    chunks = []

    # Build conversation blocks (group 3-5 messages for context)
    block_size = 4
    for i in range(0, len(extracted), block_size):
        block = extracted[i:i + block_size]

        # Format as conversation
        lines = []
        for msg in block:
            role_label = {"user": "Human", "assistant": "Agent", "toolResult": "Tool"}.get(
                msg["role"], msg["role"].capitalize()
            )
            lines.append(f"[{role_label}]: {msg['text']}")

        full_text = "\n\n".join(lines)

        # Timestamp range
        ts_start = block[0].get("timestamp", "")
        ts_end = block[-1].get("timestamp", "")
        roles_in_block = list(set(m["role"] for m in block))

        # Split into chunks if too long
        if len(full_text) <= chunk_size:
            chunks.append({
                "text": full_text,
                "ts_start": ts_start,
                "ts_end": ts_end,
                "roles": roles_in_block,
                "msg_range": f"{i}-{i + len(block) - 1}",
            })
        else:
            # Sliding window
            pos = 0
            part_idx = 0
            while pos < len(full_text):
                end = min(pos + chunk_size, len(full_text))
                chunk_text = full_text[pos:end]
                chunks.append({
                    "text": chunk_text,
                    "ts_start": ts_start,
                    "ts_end": ts_end,
                    "roles": roles_in_block,
                    "msg_range": f"{i}-{i + len(block) - 1} (part {part_idx})",
                })
                pos += chunk_size - overlap
                part_idx += 1

    return chunks


# ─── Embedding ────────────────────────────────────────────────────────────────

def batch_embed(texts: list[str], batch_size: int = EMBED_BATCH) -> list[list[float] | None]:
    """Embed texts in batches via Ollama. Returns vectors (or None for failures)."""
    all_embeddings = [None] * len(texts)

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            r = httpx.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": batch},
                timeout=120,
            )
            r.raise_for_status()
            vecs = r.json().get("embeddings", [])
            for j, vec in enumerate(vecs):
                all_embeddings[i + j] = vec
        except Exception as e:
            print(f"\n  WARNING: Embedding batch {i // batch_size} failed: {e}")

        done = min(i + batch_size, len(texts))
        print(f"\r  Embedded {done}/{len(texts)} ({done * 100 // len(texts)}%)", end="", flush=True)

    print()
    return all_embeddings


# ─── LanceDB storage ─────────────────────────────────────────────────────────

def store_to_lancedb(records: list[dict], dry_run: bool = False) -> int:
    """Store session history records to LanceDB."""
    import lancedb

    if dry_run:
        print(f"  [DRY RUN] Would store {len(records)} records")
        for r in records[:3]:
            print(f"    {r['text'][:120]}...")
        return 0

    db = lancedb.connect(str(LANCEDB_PATH))
    tbl = db.open_table("memories")

    existing = tbl.count_rows()
    print(f"  Existing LanceDB records: {existing:,}")

    STORE_BATCH = 500
    stored = 0
    for i in range(0, len(records), STORE_BATCH):
        batch = records[i:i + STORE_BATCH]
        tbl.add(batch)
        stored += len(batch)
        print(f"\r  Stored {stored}/{len(records)}", end="", flush=True)

    print()
    new_total = tbl.count_rows()
    print(f"  New total: {new_total:,} (+{new_total - existing})")
    return stored


# ─── Agent self-summary ───────────────────────────────────────────────────────

def request_agent_summary(agent: str) -> bool:
    """Ask the agent to store their own summary of active context via Hermes."""
    print(f"\n  Requesting self-summary from {agent}...")
    try:
        r = httpx.post(
            f"{HERMES_URL}/api/v1/agent/ask",
            json={
                "caller_id": "compaction",
                "target_agent": agent,
                "message": SELF_SUMMARY_PROMPT,
                "priority": "high",
                "purpose": "session-compaction",
            },
            timeout=180,  # agent may take time to store multiple memories
        )
        if r.status_code < 400:
            resp = r.json()
            preview = str(resp.get("response", ""))[:200]
            print(f"  Agent responded: {preview}")
            return True
        else:
            print(f"  Agent summary request failed: HTTP {r.status_code}")
            return False
    except Exception as e:
        print(f"  Agent summary request error: {e}")
        return False


# ─── Session rotation ─────────────────────────────────────────────────────────

def archive_session(agent: str, session_id: str, dry_run: bool = False) -> str | None:
    """Archive the session JSONL and remove from sessions.json."""
    jsonl_path = AGENTS_DIR / agent / "sessions" / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        print(f"  Session file not found for archival: {jsonl_path}")
        return None

    # Create archive directory
    archive_dir = ARCHIVE_DIR / agent
    archive_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_name = f"{session_id}.{ts}.jsonl"
    archive_path = archive_dir / archive_name

    if dry_run:
        print(f"  [DRY RUN] Would archive {jsonl_path} → {archive_path}")
        return str(archive_path)

    # Copy (not move) — let the runtime handle the active file
    shutil.copy2(jsonl_path, archive_path)
    print(f"  Archived: {archive_path}")

    # Clear the session from sessions.json to force a fresh start
    sessions_file = AGENTS_DIR / agent / "sessions" / "sessions.json"
    try:
        with open(sessions_file) as f:
            data = json.load(f)

        main_key = f"agent:{agent}:main"
        if main_key in data:
            del data[main_key]
            with open(sessions_file, "w") as f:
                json.dump(data, f, indent=2)
            print(f"  Cleared session key '{main_key}' from sessions.json")
    except Exception as e:
        print(f"  WARNING: Could not update sessions.json: {e}")

    # Remove the active session JSONL so runtime creates a fresh one
    try:
        jsonl_path.unlink()
        print(f"  Removed active session file (fresh session on next message)")
    except Exception as e:
        print(f"  WARNING: Could not remove session file: {e}")

    return str(archive_path)


# ─── Main pipeline ────────────────────────────────────────────────────────────

def compact(agent: str, threshold: int, dry_run: bool, force: bool,
            skip_summary: bool, skip_archive: bool) -> bool:
    """Run the full compaction pipeline for one agent."""
    print(f"\n{'='*60}")
    print(f"  SESSION COMPACTION: {agent}")
    print(f"  Threshold: {threshold:,} tokens")
    print(f"{'='*60}")

    # Step 1: Check session size
    info = get_session_info(agent)
    if not info:
        print(f"  No active main session for {agent}")
        return False

    tokens = info.get("totalTokens", info.get("inputTokens", 0))
    session_id = info.get("sessionId", "")
    ctx = info.get("contextTokens", 262144)
    pct = round(tokens / ctx * 100, 1) if ctx > 0 else 0

    print(f"  Session: {session_id}")
    print(f"  Tokens:  {tokens:,} / {ctx:,} ({pct}%)")

    if not force and tokens < threshold:
        print(f"  Below threshold ({threshold:,}) — skipping")
        return False

    print(f"  ABOVE THRESHOLD — proceeding with compaction")

    # Step 2: Read and extract conversation text
    print(f"\n  Reading session messages...")
    messages = read_session_messages(agent, session_id)
    if not messages:
        print(f"  No messages found in session")
        return False

    extracted = extract_text_from_messages(messages)
    total_chars = sum(len(e["text"]) for e in extracted)
    print(f"  Extracted {len(extracted)} messages ({total_chars:,} chars)")

    # Step 3: Chunk
    print(f"\n  Chunking conversation...")
    chunks = chunk_conversation(extracted)
    print(f"  Created {len(chunks)} chunks (avg {total_chars // max(len(chunks), 1)} chars)")

    # Step 4: Embed
    print(f"\n  Embedding chunks...")
    texts = [c["text"] for c in chunks]
    embeddings = batch_embed(texts)

    # Step 5: Build LanceDB records
    now_epoch = time.time() * 1000  # LanceDB uses ms epoch for createdAt
    records = []
    for chunk, embedding in zip(chunks, embeddings):
        if embedding is None:
            continue
        records.append({
            "id": str(uuid.uuid4()),
            "text": chunk["text"],
            "vector": embedding,
            "importance": SESSION_IMPORTANCE,
            "category": SESSION_CATEGORY,
            "createdAt": now_epoch,
        })

    failed = sum(1 for e in embeddings if e is None)
    if failed:
        print(f"  WARNING: {failed} chunks failed to embed")

    print(f"  Prepared {len(records)} records for LanceDB")

    # Step 6: Store to LanceDB
    stored = store_to_lancedb(records, dry_run=dry_run)

    # Step 7: Agent self-summary (optional)
    if not skip_summary and not dry_run:
        request_agent_summary(agent)
    elif skip_summary:
        print(f"\n  Skipping agent self-summary (--skip-summary)")

    # Step 8: Archive and rotate
    if not skip_archive:
        print(f"\n  Archiving session...")
        archive_path = archive_session(agent, session_id, dry_run=dry_run)
    else:
        print(f"\n  Skipping archive (--skip-archive)")
        archive_path = None

    # Step 9: Emit event for n8n
    if not dry_run:
        try:
            httpx.post(f"{HERMES_URL}/api/v1/events", json={
                "event_type": "session.compacted",
                "source": "compact_session",
                "payload": {
                    "agent": agent,
                    "session_id": session_id,
                    "original_tokens": tokens,
                    "chunks_stored": len(records),
                    "archive_path": archive_path,
                },
            }, timeout=5)
        except Exception:
            pass  # non-critical

    # Step 10: Send reacquaintance message to the fresh session
    if not skip_archive and not dry_run:
        print(f"\n  Sending reacquaintance message to fresh session...")
        try:
            r = httpx.post(
                f"{HERMES_URL}/api/v1/agent/ask",
                json={
                    "caller_id": "compaction",
                    "target_agent": agent,
                    "message": REACQUAINT_PROMPT,
                    "priority": "normal",
                    "purpose": "session-reacquaint",
                },
                timeout=180,
            )
            if r.status_code < 400:
                resp = r.json()
                preview = str(resp.get("response", ""))[:300]
                print(f"  Agent reacquainted: {preview}")
            else:
                print(f"  Reacquaintance message failed: HTTP {r.status_code}")
                print(f"  (Agent will self-orient on next natural message)")
        except Exception as e:
            print(f"  Reacquaintance error: {e}")
            print(f"  (Agent will self-orient on next natural message)")

    print(f"\n  COMPACTION COMPLETE for {agent}")
    print(f"    Chunks embedded: {len(records)}")
    print(f"    Original tokens: {tokens:,}")
    print(f"    New session:     fresh (next message creates it)")
    return True


def main():
    parser = argparse.ArgumentParser(description="Session auto-compaction pipeline")
    parser.add_argument("--agent", type=str, help="Agent to compact (coordinator, developer, etc.)")
    parser.add_argument("--all", action="store_true", help="Compact all agents over threshold")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"Token threshold for compaction (default: {DEFAULT_THRESHOLD:,})")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--force", action="store_true", help="Compact regardless of threshold")
    parser.add_argument("--skip-summary", action="store_true", help="Skip agent self-summary step")
    parser.add_argument("--skip-archive", action="store_true", help="Skip session archival step")
    parser.add_argument("--batch", type=int, default=EMBED_BATCH, help="Embedding batch size")
    args = parser.parse_args()

    if not args.agent and not args.all:
        parser.error("Specify --agent NAME or --all")

    agents = ALL_AGENTS if args.all else [args.agent]
    compacted = 0

    for agent in agents:
        if compact(agent, args.threshold, args.dry_run, args.force,
                   args.skip_summary, args.skip_archive):
            compacted += 1

    print(f"\n{'='*60}")
    print(f"  Done. Compacted {compacted}/{len(agents)} agent(s).")
    if args.dry_run:
        print(f"  (DRY RUN — no changes written)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
