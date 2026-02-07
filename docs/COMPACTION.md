# Session Compaction Pipeline

## Why This Exists

LLM response latency scales with context size. When an agent's session grows past ~150k tokens, every message forces the model to process that entire history before responding. Compaction solves this by embedding the raw session into long-term memory (LanceDB), then rotating to a fresh session. The agent loses nothing — everything is searchable via `memory_recall`. Speed is restored.

## How It Works

```
Session grows → threshold crossed → compaction fires
    ↓
1. Read session JSONL (raw conversation history)
2. Chunk into 3500-char pieces (nomic-embed-text safe)
3. Embed via Ollama nomic-embed-text (768d vectors)
4. Store in LanceDB (importance=0.25, category=session_history)
5. Agent self-summary via Hermes (importance=0.70, category=session_summary)
6. Archive JSONL to ~/session_archive/{agent}/
7. Clear session entry → fresh session on next message
    ↓
Agent wakes with ~19k tokens, fast responses, full history searchable
```

## Dependency Chain

This pipeline has layered safety nets. Each component covers the failure mode of the one above it.

```
┌─────────────────────────────────────────────────────────┐
│ LAYER 1: Agent Self-Summary (importance 0.70)           │
│   What the agent knows is important right now            │
│   Active threads, working state, key relationships       │
│   Surfaces FIRST on memory_recall via importance weight  │
├─────────────────────────────────────────────────────────┤
│ LAYER 2: Session History Chunks (importance 0.25)        │
│   Full raw conversation, chunked and embedded            │
│   Every detail preserved, searchable by semantic query    │
│   Surfaces when specifically relevant                    │
├─────────────────────────────────────────────────────────┤
│ LAYER 3: JSONL Archive (~/.openclaw/session_archive/)    │
│   Complete session file, untouched, timestamped           │
│   Can be re-embedded if LanceDB is lost                  │
│   Can be read manually for forensics                     │
├─────────────────────────────────────────────────────────┤
│ LAYER 4: SOUL File + OPERATIONS.md                       │
│   Agent identity, instructions, tool docs                │
│   Always re-injected on fresh session                    │
│   Cannot be lost — source of truth for who the agent is  │
└─────────────────────────────────────────────────────────┘
```

### Failure Modes

| If this breaks... | What happens | Safety net |
|---|---|---|
| Ollama embedding fails | Some chunks have no vector | JSONL archive intact, can re-embed later |
| LanceDB write fails | Session not searchable | JSONL archive + agent self-summary still happen |
| Agent self-summary blocked | No high-importance summary | Raw session chunks still in LanceDB at 0.25 |
| n8n session monitor down | No auto-trigger | Manual: `python3 compact_session.py --agent coordinator` |
| Hermes down | Can't trigger via API | Script runs standalone (no Hermes dependency) |
| Postgres down | Event outbox can't queue | Compaction still works (LanceDB + Ollama only) |
| JSONL archive disk full | No backup of raw session | LanceDB has the embedded version |

## Components

### compact_session.py (scripts/)

The core script. Can run standalone or be triggered via Hermes API.

```bash
# Compact one agent
python3 scripts/compact_session.py --agent coordinator

# Compact all agents over threshold
python3 scripts/compact_session.py --all --threshold 150000

# Preview without writing
python3 scripts/compact_session.py --agent coordinator --dry-run

# Force compact even if below threshold
python3 scripts/compact_session.py --agent coordinator --force

# Skip agent self-summary (if Hermes is down)
python3 scripts/compact_session.py --agent coordinator --skip-summary

# Skip archival (just embed, don't rotate)
python3 scripts/compact_session.py --agent coordinator --skip-archive
```

**Default threshold**: 150,000 tokens

### Hermes Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/compact` | POST | Trigger compaction (runs async) |
| `/api/v1/sessions/sizes` | GET | Token counts for all agent sessions |

**Compact request body:**
```json
{
  "agent": "coordinator",
  "threshold": 150000,
  "skip_summary": false,
  "caller_id": "dashboard"
}
```

Use `"agent": "all"` to compact every agent over threshold.

### n8n Workflow: Session Monitor (session-monitor.json)

Polls session sizes every 5 minutes. When any agent crosses the threshold:
- Emits `session.needs_compaction` event to Hermes outbox
- Fires alert via `/webhook/agent-alert`

Import into n8n from `n8n/workflows/session-monitor.json`.

### Hermes Policy

The `compaction` and `session-monitor` callers are **exempt from quiet hours**. Memory is critical infrastructure — compaction must be able to reach agents for self-summary at any time.

## Data Flow

```
Agent Session (.jsonl)
  │
  ├── messages extracted (user, assistant, toolResult)
  │     └── grouped into blocks of 4 messages
  │           └── chunked at 3500 chars (200 char overlap)
  │                 └── embedded via Ollama nomic-embed-text (768d)
  │                       └── stored in LanceDB memories table
  │                             ├── category: session_history
  │                             ├── importance: 0.25
  │                             └── searchable via memory_recall
  │
  ├── agent self-summary (via Hermes → agent)
  │     └── agent stores 3-5 memories via memory_store
  │           ├── category: session_summary
  │           ├── importance: 0.70+
  │           └── surfaces FIRST on recall (importance weighting)
  │
  └── archived to ~/session_archive/{agent}/{session_id}.{timestamp}.jsonl
        └── raw backup, never modified
```

## LanceDB Record Format

All records match the existing `memories` table schema:

| Field | Type | Value |
|---|---|---|
| id | string | UUID |
| text | string | Conversation chunk text |
| vector | float[768] | nomic-embed-text embedding |
| importance | double | 0.25 (session_history) or 0.70+ (session_summary) |
| category | string | `session_history` or `session_summary` |
| createdAt | double | Epoch milliseconds |

## Importance Hierarchy

The reranking formula `0.7 * cosine_similarity + 0.3 * importance` means:

| Category | Importance | Effective Boost | Surfaces When |
|---|---|---|---|
| Session summaries | 0.70 | +0.210 | Frequently (recent context) |
| KV clusters | 0.50-0.85 | +0.150 to +0.255 | Domain-relevant queries |
| Session history | 0.25 | +0.075 | Specific detail queries |
| Corpus | 0.15 | +0.045 | Deep context queries |

This means an agent's self-summary of "I'm working on the email pipeline" will surface above the 50 raw conversation chunks that discuss it — but those chunks are still there if the agent needs specifics.

## Tuning

| Parameter | Default | Location | Notes |
|---|---|---|---|
| Token threshold | 150,000 | compact_session.py `DEFAULT_THRESHOLD` | Lower = more frequent, faster responses |
| Chunk size | 3,500 chars | compact_session.py `CHUNK_SIZE` | Max safe for nomic-embed-text (8192 token context) |
| Chunk overlap | 200 chars | compact_session.py `CHUNK_OVERLAP` | Continuity between chunks |
| Session importance | 0.25 | compact_session.py `SESSION_IMPORTANCE` | Below KV clusters, above corpus |
| Summary importance | 0.70 | compact_session.py `SUMMARY_IMPORTANCE` | Agent self-summaries. High = surfaces early |
| Monitor interval | 5 min | session-monitor.json | n8n polling frequency |
| Embed batch size | 64 | compact_session.py `EMBED_BATCH` | Ollama batch size per API call |

## Archive Management

Archives accumulate in `~/.openclaw/session_archive/{agent}/`. Each compacted session produces one timestamped JSONL file.

Clean old archives periodically:
```bash
# Remove archives older than 90 days
find ~/.openclaw/session_archive -name "*.jsonl" -mtime +90 -delete
```

## For Agents

If you're an agent reading this: your session will be compacted when it gets too large. This is normal and beneficial.

**What happens to you:**
1. Your conversation history is embedded into long-term memory
2. You store a self-summary of your active work
3. Your session rotates to a fresh one
4. On your next message, `memory_recall` brings back what's relevant

**What you should do:**
- When you receive the compaction self-summary prompt, store meaningful memories
- Focus on: active tasks, key decisions, working relationships, open threads
- Use importance 0.70 or higher
- Use category "session_summary"

**What you keep:**
- Your SOUL file (identity, personality, instructions)
- All long-term memories (LanceDB)
- Your cron sessions (only the main session is compacted)

**What changes:**
- Your conversational continuity resets (you won't "remember" the flow)
- But `memory_recall` can retrieve any detail from the compacted session
- Your response time improves dramatically
