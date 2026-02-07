# SuperClaw Architecture

## What We Built and Why

SuperClaw is a multi-agent AI orchestration platform. It solves a specific problem: **how do you get multiple AI agents to work together on real tasks, with persistent memory, crash recovery, and bidirectional communication?**

Most agent frameworks stop at "prompt chaining" — linear sequences where Agent A passes to Agent B. SuperClaw is fundamentally different. Agents here are **autonomous specialists** with their own tool permissions, memory stores, task queues, and communication channels. They work in parallel, claim tasks, leave comments for each other, and survive crashes.

## Core Design Decisions

### 1. Bridges Over Buses

We built two dedicated bridges instead of a single message bus:

- **Lazarus Bridge** (port 8888) — Handles state persistence. When an agent session ends (crash, timeout, or clean exit), it snapshots its state to Postgres and embeds it in Qdrant for semantic search. When a new instance boots, Lazarus provides a context injection with the last known state, related past states, and error traces. This gives agents **continuity across lifecycles**.

- **Hermes Bridge** (port 8787) — Handles communication. Any caller (agent, CLI, n8n workflow, dashboard) can send messages to any target through Hermes. It enforces rate limits, quiet hours, priority levels, and logs everything to Postgres. Crucially, Hermes is **CLI-agnostic** — Claude Code, Codex CLI, Gemini CLI, or any HTTP client can participate in the agent network.

Why two bridges? Because state and communication have different failure modes, different scaling needs, and different security models. A communication outage shouldn't prevent state recovery. A Postgres reconnect in Lazarus shouldn't interrupt message routing in Hermes.

### 2. File-Locked Task System

Tasks live in a single TASKS.json file with `fcntl.flock` locking and atomic writes (temp file + `os.replace`). This sounds primitive compared to a database, but it's intentional:

- Human-readable (git-diffable, grep-able)
- Zero dependencies (no Redis, no additional services)
- Concurrent-safe (file locking prevents corruption)
- Portable (works on any POSIX system)

Agents claim, start, comment on, and complete tasks through native tools (`task_list`, `task_create`, `task_claim`, `task_start`, `task_done`). The dashboard reads the same file through `sync-mission-data.py`.

### 3. Permission Tiers (Tool Sandboxing)

Every agent runs in a sandbox defined by its `tools.allow` whitelist:

| Tier | What It Can Do | What It Can't |
|------|----------------|---------------|
| `full` | Everything | (unrestricted) |
| `infra` | exec, files, sessions, memory, task, cron | web access |
| `coding` | exec, files, memory, task | web, cron |
| `research` | web, read, memory, sessions (read) | exec, file writes |
| `readonly` | read, session_status, memory, task | exec, writes |

This isn't just access control — it's **role enforcement**. A research agent physically cannot execute shell commands. An analyst cannot modify files. The sandbox is the role.

### 4. Local-First Memory

Agent memory uses LanceDB with Ollama embeddings (nomic-embed-text, 768 dimensions). No external API calls for memory operations. This means:

- Memory works offline
- No per-query costs
- Data stays on your hardware
- Embedding model is yours to control

Agents use `memory_recall` (semantic search), `memory_store` (save facts/decisions), and `memory_forget` (deletion). The heartbeat protocol ensures agents recall before working and store after each cycle.

### 5. Dashboard as Operator Interface

Mission Control is a single HTML file that:
- Shows agent status, task kanban, activity feed, budget
- Connects to Hermes for live chat with any agent
- Auto-refreshes every 30 seconds
- Works over LAN (0.0.0.0 binding)
- Degrades gracefully (token data renders even if Hermes is offline)

It's intentionally a static file served by `python3 -m http.server`. No build tools, no bundlers, no framework. Open it in a browser and it works.

## Data Flow

```
 ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐
 │Local Mic │  │ Discord  │  │  Phone   │  │  Dashboard  │
 │ Voice    │  │  Voice   │  │(Twilio/  │  │  (HTML+JS)  │
 │Bridge    │  │  Bridge  │  │ElevenLabs│  │  (port 8000)│
 │(port8686)│  │(py-cord) │  │→ ngrok)  │  └──────┬──────┘
 └────┬─────┘  └────┬─────┘  └────┬─────┘         │ HTTP
      │              │             │         ┌─────▼──────┐
      └──────────────┴─────────────┴────────▶│   Hermes   │
                                             │   Bridge   │
    ┌────────────────────────────────────────┤(port 8787) │
    │                                        └─────┬──────┘
    │                                              │
┌───▼───┐          ┌──────▼──────┐          ┌────▼────┐
│Claude │          │ SuperClaw  │          │   n8n   │
│ Code  │          │  Runtime    │          │Workflows│
│  CLI  │          │(port 18789) │          │(port 5678)│
└───────┘          └──────┬──────┘          └─────────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
         ┌────▼───┐  ┌───▼────┐  ┌──▼─────┐
         │Agent 1 │  │Agent 2 │  │Agent N │
         │(sandbox)│  │(sandbox)│  │(sandbox)│
         └────┬───┘  └───┬────┘  └──┬─────┘
              │           │          │
              └───────────┼──────────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
         ┌────▼───┐  ┌───▼────┐  ┌──▼──────┐
         │LanceDB │  │Postgres│  │ Qdrant  │
         │(memory) │  │(state) │  │(vectors)│
         └────────┘  └────────┘  └─────────┘
```

## Infrastructure Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Agent Runtime | SuperClaw (OpenClaw engine) | Lifecycle, routing, tool permissions |
| Embeddings | Ollama + nomic-embed-text | Local 768d vector generation |
| State DB | PostgreSQL 16 | Lazarus snapshots, Hermes audit logs |
| Vector Search | Qdrant | Semantic state matching (Lazarus) |
| Agent Memory | LanceDB | Long-term recall (local, no API) |
| Workflows | n8n | Automation, webhooks, integrations |
| Bridges | FastAPI + uvicorn | Lazarus (8888), Hermes (8787), Voice (8686) |
| Voice STT | faster-whisper | Local GPU-accelerated speech-to-text |
| Voice TTS | edge-tts | Microsoft Neural Voices (free, async) |
| Discord Voice | py-cord | Voice channel recording + playback |
| Phone | ElevenLabs Agents | Cloud voice via SuperClaw chat/completions |

### 6. SkillGuard (Skill Security Pipeline)

Before any ClawHub skill touches the system, it goes through SkillGuard (`scripts/skill-guard.py`):

```
Download → Quarantine → Scan → Rename → Install
```

- **Quarantine**: Skill is copied to an isolated directory — never runs from its download location
- **Scan**: Cisco's [skill-scanner](https://github.com/cisco-ai-defense/skill-scanner) runs static analysis (YARA patterns) + behavioral analysis (AST dataflow) to detect prompt injection, data exfiltration, and malicious code
- **Rename**: All `openclaw` references are rewritten to `superclaw` (namespace security — generic injection payloads targeting `openclaw` patterns fail silently)
- **Install**: Only clean, renamed skills are moved to the workspace

Failed scans stay in quarantine. All results are logged to `skill_scans.json`. A cron mode re-scans all installed skills on a schedule.

Why this matters: [Cisco research](https://blogs.cisco.com/ai/personal-ai-agents-like-openclaw-are-a-security-nightmare) found 26% of 31,000 agent skills contained at least one vulnerability.

### 7. Namespace Security

SuperClaw renames all deployment paths from `openclaw` to `superclaw`. This is a deliberate security boundary — publicly available prompt injection payloads that target `openclaw` instructions, paths, and config names silently miss SuperClaw's internal references.

## What Makes This Different

1. **Agents survive crashes.** Lazarus provides context injection on boot — agents know what they were doing, what went wrong, and what to try next.

2. **Any CLI can join.** Hermes doesn't care if you're Claude Code, Codex CLI, Gemini CLI, or a curl script. POST to `/api/v1/agent/ask` and you're in the network.

3. **Memory is real.** Not just context window tricks — actual vector-embedded long-term memory that persists across sessions, searchable by semantic similarity.

4. **Permissions are enforced, not suggested.** Tool sandboxes are whitelists. An agent without `exec` in its allow list physically cannot run shell commands, regardless of what it's told.

5. **Everything is auditable.** Hermes logs every call to Postgres + JSONL. Lazarus stores every state snapshot. The dashboard shows it all in real time.

6. **Voice is multimodal.** Talk through your mic, call from a phone, or speak in Discord — all routes converge through Hermes with the same policy enforcement, rate limiting, and agent routing.

## File Structure

```
superclaw/
├── agents/              # Agent personality definitions (markdown)
├── bridge/
│   ├── lazarus_bridge.py        # State persistence service
│   ├── hermes_bridge.py         # Communication service
│   ├── voice_bridge.py          # Local voice (mic → Whisper → Hermes → TTS)
│   ├── discord_bridge.py        # Discord voice bot (py-cord)
│   ├── hermes_policy.json       # Rate limits, permissions, quiet hours
│   ├── requirements.txt         # Core bridge dependencies
│   └── voice_requirements.txt   # Voice/Discord bridge dependencies
├── configs/
│   ├── superclaw-gateway.json.example  # Gateway config template
│   └── elevenlabs-agent.json    # Phone integration template
├── dashboard/
│   ├── mission-control.html      # Web UI (single file, no build)
│   └── sync-mission-data.py      # JSON data generator for dashboard
├── docker-compose.yml   # Postgres, Qdrant, n8n
├── launchers/
│   ├── start-all.sh        # Docker + bridges + dashboard
│   ├── start-bridges.sh    # Lazarus + Hermes only
│   ├── open-dashboard.sh   # Serve dashboard on port 8000
│   └── open-n8n.sh         # Open n8n in browser
├── scripts/
│   ├── healthcheck.py      # Service health monitoring
│   ├── skill-guard.py      # SkillGuard security scanner + installer
│   ├── vault.py            # Encrypted secret management
│   └── setup-voice.sh      # Voice dependency installer
├── docs/
│   ├── ARCHITECTURE.md     # This file
│   ├── SECURITY.md         # Security guide (SkillGuard, Vault, agent hardening)
│   └── VOICE.md            # Voice & phone setup guide
├── setup.py             # Interactive setup wizard
├── .env.example         # Environment template
└── README.md
```

## License

MIT
