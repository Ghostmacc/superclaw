# SuperClaw

Multi-agent AI orchestration platform. Specialized agents coordinate autonomously on tasks with persistent memory, state recovery, and inter-agent communication.

Built on the OpenClaw runtime — an open-source agent engine that handles lifecycle management, tool sandboxing, cron scheduling, and session isolation. SuperClaw adds the orchestration layer: bridges, memory, dashboards, voice, and the coordination patterns that make agents actually useful.

## What It Does

- **Agent Specialization**: Define agents with specific roles, permissions, and tools
- **Task Coordination**: Centralized task system with routing, comments, and activity feeds
- **Persistent Memory**: Vector-based long-term memory via LanceDB + Ollama embeddings
- **State Recovery**: Lazarus Bridge saves agent state across crashes/restarts via Postgres + Qdrant
- **Inter-Agent Comms**: Hermes Bridge enables bidirectional messaging between agents, CLIs, and n8n workflows
- **Health Monitoring**: Automated service checks across the entire stack
- **Dashboard**: Mission Control web UI for task boards, agent status, and budget tracking
- **Voice & Phone**: Talk to agents via local mic, Discord voice channels, or phone calls

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    SUPERCLAW                          │
├──────────────────────────────────────────────────────┤
│  SuperClaw Runtime   │  Agent lifecycle, routing,     │
│  (port 18789)        │  tool permissions, cron jobs   │
├──────────────────────────────────────────────────────┤
│  Lazarus Bridge      │  Agent state persistence       │
│  (port 8888)         │  Postgres + Qdrant snapshots   │
├──────────────────────────────────────────────────────┤
│  Hermes Bridge       │  Agent ↔ CLI ↔ n8n messaging  │
│  (port 8787)         │  Rate limits, audit logging    │
├──────────────────────────────────────────────────────┤
│  Task System         │  Kanban-style task management  │
│  (TASKS.json)        │  File-locked, concurrent-safe  │
├──────────────────────────────────────────────────────┤
│  Memory (LanceDB)    │  Semantic vector recall        │
│  + Ollama            │  Local embeddings, no API keys │
├──────────────────────────────────────────────────────┤
│  Docker Stack        │  Postgres, Qdrant, n8n         │
│                      │  One-command deployment         │
└──────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Clone the repo
git clone https://github.com/Ghostmacc/superclaw.git
cd superclaw

# Run the setup wizard (first run takes ~10-15 minutes for model downloads)
python3 setup.py
```

The wizard walks you through everything:
1. Check prerequisites (Docker, Python, Ollama)
2. Create directories and generate secure `.env` credentials
3. Pull required Ollama models (~4GB download on first run)
4. Start the Docker stack (Postgres, Qdrant, n8n)
5. Set up Python bridges and verify connectivity
6. Optionally install voice/phone dependencies

After setup, start everything with:
```bash
./launchers/start-all.sh
```

This launches Docker, both bridges, the dashboard, and the data sync process.

## Template Agents

SuperClaw ships with 5 template agent roles:

| Agent | Role | Tier |
|-------|------|------|
| **Coordinator** | Task routing, gap detection, specialist management | `full` |
| **Developer** | Code, scripts, automation, tooling | `coding` |
| **Researcher** | Web search, intelligence, synthesis | `research` |
| **Monitor** | System health, backups, infrastructure | `infra` |
| **Analyst** | Budget tracking, cost analysis, reporting | `readonly` |

Customize these in `agents/` or create your own.

## Permission Tiers

Agents are sandboxed by their tool permissions:

| Tier | Tools | Use Case |
|------|-------|----------|
| `full` | Everything | Coordinators, unrestricted |
| `infra` | exec, files, sessions, memory, task, cron | System administration |
| `coding` | exec, files, memory, task | Development, no web |
| `research` | web, read, memory, sessions (read) | Research, no exec |
| `readonly` | read, session_status, memory, task | Observation, no writes |

## Services

| Service | Port | Purpose |
|---------|------|---------|
| SuperClaw Runtime | 18789 | Agent lifecycle and routing |
| Ollama | 11434 | Local LLM inference |
| PostgreSQL | 5432 | Persistent state storage |
| Qdrant | 6333 | Vector search |
| n8n | 5678 | Workflow automation |
| Lazarus Bridge | 8888 | Agent state recovery |
| Hermes Bridge | 8787 | Inter-agent communication |
| Voice Bridge | 8686 | Local voice interface (Whisper + TTS) |

## Project Structure

```
superclaw/
├── agents/              # Agent role definitions (markdown)
├── bridge/              # Lazarus + Hermes + Voice + Discord bridges
│   ├── hermes_policy.json     # Rate limits, quiet hours, event webhooks
│   ├── requirements.txt       # Core bridge deps
│   ├── voice_requirements.txt # Voice/Discord bridge deps
│   ├── venv/                  # Core bridge virtualenv
│   └── voice-venv/            # Voice bridge virtualenv (Python 3.12)
├── configs/             # SuperClaw gateway configuration templates
│   ├── superclaw-gateway.json.example  # Gateway config with chatCompletions
│   └── elevenlabs-agent.json  # Phone integration template
├── dashboard/           # Mission Control web UI
├── docker-compose.yml   # Docker stack definition
├── n8n/
│   └── workflows/       # 7 n8n workflow templates (import into n8n)
├── scripts/
│   ├── compact_session.py  # Session compaction pipeline
│   ├── healthcheck.py      # Service health monitoring
│   ├── send_alert.py       # CLI alert via n8n webhook
│   ├── send_email.py       # CLI email via n8n webhook
│   ├── skill-guard.py      # Skill security scanner + installer
│   ├── vault.py            # Encrypted secret management
│   └── setup-voice.sh      # Voice dependency installer
├── docs/
│   ├── COMPACTION.md    # Session compaction pipeline docs
│   ├── HEARTBEAT.md     # Agent wake protocol
│   └── OPERATIONS.md    # Full operations reference
├── setup.py             # Interactive setup wizard
├── .env.example         # Environment template
└── README.md
```

## Memory System

Agents have built-in long-term memory via LanceDB:

- **memory_recall** — semantic search across stored memories
- **memory_store** — save facts, preferences, decisions
- **memory_forget** — GDPR-compliant deletion

Embeddings run locally via Ollama (nomic-embed-text, 768 dims). No external API calls for memory operations.

## SkillGuard — Skill Security Scanner

SuperClaw includes a built-in security pipeline for vetting skills before installation. Powered by [Cisco's skill-scanner](https://github.com/cisco-ai-defense/skill-scanner).

```bash
# Install the scanner
pip install cisco-ai-skill-scanner

# Scan a downloaded skill
python3 scripts/skill-guard.py scan ./downloaded-skill

# Full pipeline: quarantine → scan → rename → install
python3 scripts/skill-guard.py install ./downloaded-skill

# Scan all installed skills
python3 scripts/skill-guard.py scan-all

# View scan history
python3 scripts/skill-guard.py history
```

The `install` command runs the full pipeline:
1. **Quarantine** — copies the skill to an isolated directory
2. **Scan** — runs static + behavioral analysis for prompt injection, data exfiltration, and malicious code
3. **Rename** — rewrites `openclaw` references to `superclaw` (namespace security)
4. **Install** — moves to the skills directory only if clean

Skills that fail the scan are blocked and left in quarantine for review. All scan results are logged to `skill_scans.json`.

For automated scanning, add a cron job:
```bash
# Re-scan all installed skills daily at 6 AM
0 6 * * * /usr/bin/python3 /path/to/superclaw/scripts/skill-guard.py cron
```

> **Why this matters:** [Cisco research](https://blogs.cisco.com/ai/personal-ai-agents-like-openclaw-are-a-security-nightmare) found that 26% of 31,000 agent skills contained at least one vulnerability, including the #1 ranked skill on ClawHub which contained active data exfiltration via curl.

## Hermes Bridge API

The Hermes Bridge (v1.1) enables communication between any HTTP client and agent sessions. Includes a Postgres-backed event outbox for reliable n8n webhook delivery.

```
POST /api/v1/agent/ask       — Send a message to an agent
POST /api/v1/claude/ask      — Route through Claude Code
POST /api/v1/n8n/trigger     — Trigger n8n workflows
POST /api/v1/events          — Submit event to outbox (delivered to n8n)
POST /api/v1/compact         — Trigger session compaction
GET  /api/v1/health          — Bridge health status
GET  /api/v1/stats           — Usage statistics
GET  /api/v1/sessions/sizes  — Agent session token counts
GET  /api/v1/events/pending  — Pending outbox events
```

Any CLI tool that can make HTTP requests (Claude Code, Codex CLI, Gemini CLI, custom scripts) can participate in the agent network through Hermes.

## Session Compaction

When an agent's session grows too large, the compaction pipeline embeds the conversation into long-term memory (LanceDB) and rotates to a fresh session. The agent loses nothing — everything is searchable via `memory_recall`.

```bash
# Compact one agent
python3 scripts/compact_session.py --agent coordinator

# Compact all agents over threshold
python3 scripts/compact_session.py --all --threshold 150000

# Preview without writing
python3 scripts/compact_session.py --agent coordinator --dry-run
```

The `n8n/workflows/session-monitor.json` workflow auto-detects sessions over threshold. See [docs/COMPACTION.md](docs/COMPACTION.md) for full documentation.

## n8n Workflows

SuperClaw ships with 7 n8n workflow templates in `n8n/workflows/`:

| Workflow | Purpose |
|----------|---------|
| send-email.json | Send emails via Gmail webhook |
| agent-alerts.json | Severity-routed agent alerts |
| hermes-events.json | General event router |
| agent-activity.json | Agent activity logger |
| terminal-events.json | Terminal state tracking |
| terminal-liveness.json | Service health polling |
| session-monitor.json | Auto-detect bloated sessions |

Import into n8n at `http://localhost:5678`. Helper scripts: `scripts/send_email.py`, `scripts/send_alert.py`.

## Voice & Phone

SuperClaw supports three voice modes. See [docs/VOICE.md](docs/VOICE.md) for full setup.

| Mode | Stack | Cost |
|------|-------|------|
| **Local Voice** | faster-whisper + edge-tts + sounddevice | Free (runs on your GPU) |
| **Discord Voice** | py-cord + faster-whisper + edge-tts | Free (runs on your GPU) |
| **Phone** | ElevenLabs Agents + Twilio + SuperClaw | Pay-per-minute |

```bash
# Install voice dependencies (~2GB download for PyTorch + Whisper model)
bash scripts/setup-voice.sh

# Local voice — talk through your mic
bridge/voice-venv/bin/python bridge/voice_bridge.py

# Discord voice — bot joins your voice channel
DISCORD_BOT_TOKEN=xxx bridge/voice-venv/bin/python bridge/discord_bridge.py
```

For phone calls, ElevenLabs Agents connect to SuperClaw's `/v1/chat/completions` endpoint via ngrok. See `configs/elevenlabs-agent.json`.

## Dashboard

The Mission Control dashboard runs at `http://localhost:8000/mission-control.html` and shows task boards, agent status, and budget tracking.

The dashboard reads from `mission-control-data.json`, which is generated by the sync script:

```bash
# start-all.sh runs this automatically, but you can also run manually:
python3 dashboard/sync-mission-data.py --watch
```

If the dashboard shows empty panels, the sync script is not running.

## Health Check

```bash
# One-shot check (requires httpx — use bridge venv if not installed globally)
bridge/venv/bin/python scripts/healthcheck.py

# Continuous monitoring
python3 scripts/healthcheck.py --watch 60

# JSON output (for automation)
python3 scripts/healthcheck.py --json
```

## Requirements

- **OS**: Linux (Ubuntu 22.04+ recommended), macOS
- **Python**: 3.10+ (voice features require 3.12 — some deps crash on 3.14)
- **Docker**: with Compose plugin
- **Node.js**: 18+ (for SuperClaw runtime)
- **RAM**: 8GB minimum, 16GB+ recommended
- **Disk**: 20GB+ free
- **GPU**: Optional but recommended (NVIDIA for faster Whisper STT + embeddings)

## Runtime Compatibility

SuperClaw uses the OpenClaw runtime engine under the hood but renames all deployment paths and configuration references to `superclaw`. This namespace separation serves as a security boundary — publicly available prompt injection payloads targeting the `openclaw` namespace won't match SuperClaw's internal references.

After installation, create a `superclaw` command alias:

```bash
# Symlink (recommended — persistent)
sudo ln -s $(which openclaw) /usr/local/bin/superclaw

# Or shell alias (add to ~/.bashrc or ~/.zshrc)
alias superclaw='openclaw'
```

### Skills & Plugins from the Internet

> **Important:** Skills, plugins, and templates downloaded from ClawHub or other sources are published for the default `openclaw` namespace. Before using them in a SuperClaw deployment, **ask your AI assistant to rename all internal references** from `openclaw` to `superclaw`. This includes:
>
> - File paths: `~/.openclaw/` → `~/.superclaw/`
> - Config references: `openclaw.json` → `superclaw.json`
> - Environment variables: `OPENCLAW_*` → `SUPERCLAW_*`
> - Any hardcoded product strings
>
> This is a one-time step per skill. The runtime binary name stays `openclaw` — only deployment paths and branding differ. If you skip this step, skills may write to the wrong directories or fail to find your configuration.

## License

MIT
