#!/usr/bin/env python3
"""
SuperClaw Setup Wizard

Interactive setup for the SuperClaw multi-agent orchestration platform.
Checks prerequisites, pulls models, starts services, and verifies health.

Usage:
  python3 setup.py              # interactive setup
  python3 setup.py --check      # verify existing installation
  python3 setup.py --minimal    # skip optional components
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ─── Constants ────────────────────────────────────────────────────────────────

SUPERCLAW_DIR = Path(__file__).parent.resolve()
HOME = Path.home()
SUPERCLAW_HOME = HOME / ".superclaw"
WORKSPACE_DIR = SUPERCLAW_HOME / "workspace"
MEMORY_DIR = SUPERCLAW_HOME / "memory"

REQUIRED_OLLAMA_MODELS = ["nomic-embed-text"]
RECOMMENDED_OLLAMA_MODELS = ["qwen3:8b"]

REQUIRED_PORTS = {
    5432: "PostgreSQL",
    5678: "n8n",
    6333: "Qdrant",
    8787: "Hermes Bridge",
    8888: "Lazarus Bridge",
    11434: "Ollama",
}

BANNER = """
╔══════════════════════════════════════════════════╗
║             SUPERCLAW SETUP WIZARD               ║
║     Multi-Agent Orchestration Platform           ║
╚══════════════════════════════════════════════════╝
"""


# ─── Utilities ────────────────────────────────────────────────────────────────

class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg):
    print(f"  {Colors.GREEN}[OK]{Colors.END} {msg}")


def warn(msg):
    print(f"  {Colors.YELLOW}[!!]{Colors.END} {msg}")


def fail(msg):
    print(f"  {Colors.RED}[XX]{Colors.END} {msg}")


def info(msg):
    print(f"  {Colors.CYAN}[..]{Colors.END} {msg}")


def header(msg):
    print(f"\n{Colors.BOLD}{'─'*54}")
    print(f"  {msg}")
    print(f"{'─'*54}{Colors.END}")


def ask(prompt, default=None):
    """Prompt user for input with optional default."""
    if default:
        result = input(f"  {prompt} [{default}]: ").strip()
        return result if result else default
    return input(f"  {prompt}: ").strip()


def ask_yn(prompt, default=True):
    """Yes/no prompt."""
    suffix = "[Y/n]" if default else "[y/N]"
    result = input(f"  {prompt} {suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def run(cmd, check=True, capture=True, timeout=60):
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd, shell=True, check=check,
            capture_output=capture, text=True, timeout=timeout,
        )
        return result.stdout.strip() if capture else ""
    except subprocess.CalledProcessError as e:
        return None
    except subprocess.TimeoutExpired:
        return None


def cmd_exists(name):
    """Check if a command exists on PATH."""
    return shutil.which(name) is not None


def generate_secret(length=32):
    """Generate a secure random string."""
    return secrets.token_urlsafe(length)


def check_port(port):
    """Check if a port is available."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0


# ─── Phase 1: Prerequisites ──────────────────────────────────────────────────

def check_prerequisites():
    """Check that all required tools are installed."""
    header("Phase 1: Prerequisites")
    issues = []

    # Python
    py_ver = sys.version_info
    if py_ver >= (3, 10):
        ok(f"Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    else:
        fail(f"Python {py_ver.major}.{py_ver.minor} — need 3.10+")
        issues.append("python")

    # Docker
    if cmd_exists("docker"):
        docker_ver = run("docker --version")
        ok(f"Docker: {docker_ver}")
        # Check Docker is running
        if run("docker info", check=False) is not None:
            ok("Docker daemon is running")
        else:
            fail("Docker is installed but not running")
            issues.append("docker-daemon")
    else:
        fail("Docker not found — install from https://docs.docker.com/get-docker/")
        issues.append("docker")

    # Docker Compose
    if run("docker compose version", check=False) is not None:
        ok("Docker Compose (plugin)")
    elif cmd_exists("docker-compose"):
        ok("Docker Compose (standalone)")
    else:
        fail("Docker Compose not found")
        issues.append("docker-compose")

    # Node/npm (for SuperClaw runtime)
    if cmd_exists("node"):
        node_ver = run("node --version")
        ok(f"Node.js {node_ver}")
    else:
        warn("Node.js not found — needed for SuperClaw runtime")
        issues.append("node")

    if cmd_exists("npm"):
        ok("npm available")
    else:
        warn("npm not found — needed for SuperClaw installation")
        issues.append("npm")

    # SuperClaw runtime (openclaw engine)
    if cmd_exists("openclaw"):
        oc_ver = run("openclaw --version", check=False)
        ok(f"SuperClaw runtime: {oc_ver or 'installed'}")
    else:
        warn("SuperClaw runtime not found — install with: npm install -g openclaw-gateway")

    # Ollama
    if cmd_exists("ollama"):
        ok("Ollama installed")
    else:
        warn("Ollama not found — will attempt to install")
        issues.append("ollama")

    # Git
    if cmd_exists("git"):
        ok("Git available")
    else:
        warn("Git not found — recommended for version control")

    # Disk space
    import shutil as sh
    total, used, free = sh.disk_usage(str(HOME))
    free_gb = free / (1024**3)
    if free_gb > 20:
        ok(f"Disk space: {free_gb:.1f} GB free")
    elif free_gb > 10:
        warn(f"Disk space: {free_gb:.1f} GB free (20GB+ recommended)")
    else:
        fail(f"Disk space: {free_gb:.1f} GB free (need 10GB+)")
        issues.append("disk")

    # GPU check (optional)
    gpu = run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", check=False)
    if gpu:
        ok(f"GPU: {gpu}")
    else:
        info("No NVIDIA GPU detected — CPU mode will be used for embeddings")

    return issues


# ─── Phase 2: Install Missing Tools ──────────────────────────────────────────

def install_missing(issues):
    """Attempt to install missing prerequisites."""
    if not issues:
        return True

    header("Phase 2: Installing Missing Components")

    if "ollama" in issues:
        if ask_yn("Install Ollama?"):
            info("Installing Ollama...")
            result = run("curl -fsSL https://ollama.com/install.sh | sh", check=False, capture=False, timeout=120)
            if cmd_exists("ollama"):
                ok("Ollama installed")
            else:
                fail("Ollama installation failed — install manually: https://ollama.com")
                return False

    blocking = [i for i in issues if i in ("python", "docker", "docker-daemon", "disk")]
    if blocking:
        fail(f"Blocking issues remain: {', '.join(blocking)}")
        print("  Please resolve these before continuing.")
        return False

    return True


# ─── Phase 3: Directory Structure ─────────────────────────────────────────────

def setup_directories():
    """Create the required directory structure."""
    header("Phase 3: Directory Structure")

    dirs = [
        SUPERCLAW_HOME,
        WORKSPACE_DIR,
        WORKSPACE_DIR / "memory" / "squad",
        WORKSPACE_DIR / "ui",
        WORKSPACE_DIR / "agents",
        MEMORY_DIR / "lancedb",
        SUPERCLAW_HOME / "agents",
        SUPERCLAW_HOME / "cron",
        SUPERCLAW_HOME / "logs",
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    ok(f"Created {len(dirs)} directories under {SUPERCLAW_HOME}")

    # Copy agent SOUL files
    agents_src = SUPERCLAW_DIR / "agents"
    agents_dst = WORKSPACE_DIR / "agents"
    if agents_src.exists():
        count = 0
        for f in agents_src.glob("*.md"):
            dst = agents_dst / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
                count += 1
        if count:
            ok(f"Copied {count} agent templates to workspace")
        else:
            info("Agent templates already in place")


# ─── Phase 4: Environment Configuration ──────────────────────────────────────

def setup_environment():
    """Generate .env with secure credentials."""
    header("Phase 4: Environment Configuration")

    env_file = SUPERCLAW_DIR / ".env"

    if env_file.exists():
        if not ask_yn("Existing .env found. Regenerate?", default=False):
            ok("Keeping existing .env")
            return
        # Back up existing
        backup = env_file.with_suffix(".env.backup")
        shutil.copy2(env_file, backup)
        info(f"Backed up to {backup.name}")

    # Generate secure values
    pg_user = ask("Postgres username", "superclaw")
    pg_pass = generate_secret(24)
    pg_db = ask("Postgres database name", "superclaw")
    n8n_key = generate_secret(32)
    n8n_jwt = generate_secret(32)

    env_content = f"""# SuperClaw Environment — generated by setup.py
# WARNING: Contains secrets. Do not commit to git.
# See .env.example for all available options (voice, Discord, advanced tuning).

# Database
POSTGRES_USER={pg_user}
POSTGRES_PASSWORD={pg_pass}
POSTGRES_DB={pg_db}
POSTGRES_PORT=5432

# n8n Workflow Engine
N8N_ENCRYPTION_KEY={n8n_key}
N8N_JWT_SECRET={n8n_jwt}
N8N_PORT=5678

# Qdrant Vector Store
QDRANT_HOST=localhost
QDRANT_PORT=6333

# Ollama
OLLAMA_URL=http://localhost:11434
EMBED_MODEL=nomic-embed-text

# Bridges
LAZARUS_PORT=8888
HERMES_PORT=8787
HERMES_URL=http://localhost:8787
N8N_BASE_URL=http://localhost:5678
POSTGRES_DSN=postgresql://{pg_user}:{pg_pass}@localhost:5432/{pg_db}
"""

    env_file.write_text(env_content)
    os.chmod(env_file, 0o600)
    ok(f"Generated .env with secure credentials (mode 600)")


# ─── Phase 5: SuperClaw Configuration ────────────────────────────────────────

def setup_superclaw_config():
    """Copy gateway config template and generate auth token."""
    header("Phase 5: SuperClaw Configuration")

    config_dst = SUPERCLAW_HOME / "superclaw.json"
    config_src = SUPERCLAW_DIR / "configs" / "superclaw-gateway.json.example"

    if config_dst.exists():
        if not ask_yn("Existing gateway config found. Overwrite?", default=False):
            ok("Keeping existing config")
            return
        backup = config_dst.with_suffix(".json.backup")
        shutil.copy2(config_dst, backup)
        info(f"Backed up to {backup}")

    if not config_src.exists():
        warn("configs/superclaw-gateway.json.example not found — skipping gateway config")
        return

    config_text = config_src.read_text()
    token = secrets.token_hex(24)
    config_text = config_text.replace("GENERATE_A_SECURE_TOKEN_HERE", token)

    config_dst.write_text(config_text)
    os.chmod(config_dst, 0o600)
    ok(f"Gateway config → {config_dst}")
    ok("Auth token generated and saved")
    info("Chat completions endpoint enabled (for ElevenLabs phone integration)")
    info("Discord text: run 'superclaw onboard' after setup to connect a bot")


# ─── Phase 6: Ollama Models ──────────────────────────────────────────────────

def setup_ollama():
    """Start Ollama and pull required models."""
    header("Phase 6: Ollama Models")

    # Check if Ollama is running
    try:
        import httpx
    except ImportError:
        warn("httpx not installed — installing now...")
        run(f"{sys.executable} -m pip install httpx --quiet", check=False, timeout=60)
        try:
            import httpx
        except ImportError:
            fail("Could not install httpx. Run: pip install httpx")
            return
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
        ok("Ollama is running")
    except Exception:
        info("Starting Ollama...")
        run("ollama serve &", check=False, capture=False)
        time.sleep(3)
        try:
            httpx.get("http://localhost:11434/api/tags", timeout=5)
            ok("Ollama started")
        except Exception:
            warn("Could not start Ollama — you may need to start it manually")
            return

    # Check installed models
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
        installed = [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        installed = []

    # Pull required models
    for model in REQUIRED_OLLAMA_MODELS:
        if any(model in m for m in installed):
            ok(f"Model: {model} (already installed)")
        else:
            info(f"Pulling {model} (required for memory system)...")
            run(f"ollama pull {model}", check=False, capture=False, timeout=300)
            ok(f"Pulled {model}")

    # Offer recommended models
    for model in RECOMMENDED_OLLAMA_MODELS:
        if any(model in m for m in installed):
            ok(f"Model: {model} (already installed)")
        else:
            if ask_yn(f"Pull {model}? (recommended for agent reasoning)"):
                info(f"Pulling {model}...")
                run(f"ollama pull {model}", check=False, capture=False, timeout=600)
                ok(f"Pulled {model}")
            else:
                info(f"Skipped {model}")


# ─── Phase 7: Docker Stack ───────────────────────────────────────────────────

def setup_docker():
    """Start the Docker services."""
    header("Phase 7: Docker Services")

    compose_file = SUPERCLAW_DIR / "docker-compose.yml"
    if not compose_file.exists():
        fail("docker-compose.yml not found")
        return False

    # Check ports
    blocked = []
    for port, name in REQUIRED_PORTS.items():
        if port in (8787, 8888, 11434):
            continue  # these are non-Docker services
        if not check_port(port):
            blocked.append(f"{name} (:{port})")

    if blocked:
        warn(f"Ports in use: {', '.join(blocked)}")
        if not ask_yn("Continue anyway? (existing services may conflict)"):
            return False

    info("Starting Docker stack...")
    result = run(
        f"docker compose -f {compose_file} up -d",
        check=False, capture=False, timeout=120,
    )

    # Wait for health
    info("Waiting for services to be healthy...")
    for i in range(30):
        time.sleep(2)
        pg_ok = not check_port(5432)
        qd_ok = not check_port(6333)
        if pg_ok and qd_ok:
            ok("PostgreSQL is up")
            ok("Qdrant is up")
            return True

    warn("Services may still be starting — check with: docker compose ps")
    return True


# ─── Phase 8: Python Bridges ─────────────────────────────────────────────────

def setup_bridges():
    """Set up Python virtual environment and bridge dependencies."""
    header("Phase 8: Bridge Setup")

    bridge_dir = SUPERCLAW_DIR / "bridge"
    venv_dir = bridge_dir / "venv"
    req_file = bridge_dir / "requirements.txt"

    if not req_file.exists():
        warn("bridge/requirements.txt not found — skipping bridge setup")
        return

    # Create venv
    if not venv_dir.exists():
        info("Creating Python virtual environment...")
        run(f"{sys.executable} -m venv {venv_dir}", check=False)
        ok(f"Created venv at bridge/venv/")
    else:
        ok("Virtual environment exists")

    # Install requirements
    pip = venv_dir / "bin" / "pip"
    if pip.exists():
        info("Installing bridge dependencies...")
        run(f"{pip} install -r {req_file}", check=False, capture=False, timeout=120)
        ok("Bridge dependencies installed")
    else:
        fail("Could not find pip in venv")

    # Check for bridge files
    lazarus = bridge_dir / "lazarus_bridge.py"
    hermes = bridge_dir / "hermes_bridge.py"

    if lazarus.exists():
        ok("Lazarus Bridge found")
    else:
        warn("lazarus_bridge.py not found — copy from your bridge implementation")

    if hermes.exists():
        ok("Hermes Bridge found")
    else:
        warn("hermes_bridge.py not found — copy from your bridge implementation")

    # Offer systemd setup
    if cmd_exists("systemctl") and os.geteuid() == 0:
        if ask_yn("Install systemd services for bridges?"):
            install_systemd_services(bridge_dir, venv_dir)
    else:
        info("Run bridges manually or set up systemd services (see docs/ARCHITECTURE.md)")


def install_systemd_services(bridge_dir, venv_dir):
    """Install systemd service files for bridges."""
    user = os.getenv("USER", "superclaw")
    env_file = SUPERCLAW_DIR / ".env"

    # Read DSN from .env
    dsn = "postgresql://superclaw:password@localhost:5432/superclaw"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("POSTGRES_DSN="):
                dsn = line.split("=", 1)[1]

    services = {
        "superclaw-lazarus": {
            "description": "SuperClaw Lazarus Bridge - Agent State Persistence",
            "script": "lazarus_bridge.py",
            "after": "network.target docker.service",
            "env_extra": f'Environment="POSTGRES_DSN={dsn}"\nEnvironment="QDRANT_HOST=localhost"\nEnvironment="OLLAMA_URL=http://localhost:11434"\nEnvironment="EMBED_MODEL=nomic-embed-text"',
        },
        "superclaw-hermes": {
            "description": "SuperClaw Hermes Bridge - Inter-Agent Communication",
            "script": "hermes_bridge.py",
            "after": "superclaw-lazarus.service",
            "env_extra": f'Environment="POSTGRES_DSN={dsn}"\nEnvironment="N8N_BASE_URL=http://localhost:5678"',
        },
    }

    for name, cfg in services.items():
        unit = f"""[Unit]
Description={cfg['description']}
After={cfg['after']}

[Service]
Type=simple
User={user}
WorkingDirectory={bridge_dir}
ExecStart={venv_dir}/bin/python {cfg['script']}
{cfg['env_extra']}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        svc_path = Path(f"/etc/systemd/system/{name}.service")
        svc_path.write_text(unit)
        ok(f"Installed {name}.service")

    run("systemctl daemon-reload", check=False)
    info("Enable with: systemctl enable --now superclaw-lazarus superclaw-hermes")


# ─── Phase 9: Voice Setup ────────────────────────────────────────────────────

def setup_voice():
    """Set up voice bridge dependencies (optional)."""
    header("Phase 9: Voice Setup")

    if not ask_yn("Set up voice capabilities? (local mic, Discord, phone)", default=False):
        info("Skipped — run 'bash scripts/setup-voice.sh' later to add voice")
        return

    setup_script = SUPERCLAW_DIR / "scripts" / "setup-voice.sh"
    if not setup_script.exists():
        warn("scripts/setup-voice.sh not found")
        return

    if shutil.which("python3.12"):
        ok("Python 3.12 available")
    else:
        warn("Python 3.12 not found — voice bridges require it")
        info("Install with: sudo apt install python3.12 python3.12-venv")
        if not ask_yn("Try anyway?", default=False):
            return

    info("Running voice setup script (this may take a few minutes)...")
    run(f"bash {setup_script}", check=False, capture=False, timeout=600)

    voice_venv = SUPERCLAW_DIR / "bridge" / "voice-venv"
    if voice_venv.exists() and (voice_venv / "bin" / "python").exists():
        ok("Voice environment ready at bridge/voice-venv/")
    else:
        warn("Voice setup may have had issues — check output above")
        info("Re-run anytime with: bash scripts/setup-voice.sh")


# ─── Phase 10: Start Services ────────────────────────────────────────────────

def start_services():
    """Start bridges, dashboard, and optionally voice."""
    header("Phase 10: Start Services")

    bridge_dir = SUPERCLAW_DIR / "bridge"
    dashboard_dir = SUPERCLAW_DIR / "dashboard"
    env_file = SUPERCLAW_DIR / ".env"

    # Load env vars from .env for bridge processes
    env = os.environ.copy()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip()

    venv_python = bridge_dir / "venv" / "bin" / "python"
    started = []

    # Lazarus Bridge
    lazarus = bridge_dir / "lazarus_bridge.py"
    if lazarus.exists() and venv_python.exists():
        if not check_port(8888):
            ok("Lazarus Bridge already running on :8888")
            started.append("lazarus")
        else:
            info("Starting Lazarus Bridge (port 8888)...")
            subprocess.Popen(
                [str(venv_python), str(lazarus)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, start_new_session=True,
            )
            time.sleep(3)
            if not check_port(8888):
                ok("Lazarus Bridge running on :8888")
                started.append("lazarus")
            else:
                warn("Lazarus Bridge may still be starting")

    # Hermes Bridge
    hermes = bridge_dir / "hermes_bridge.py"
    if hermes.exists() and venv_python.exists():
        if not check_port(8787):
            ok("Hermes Bridge already running on :8787")
            started.append("hermes")
        else:
            info("Starting Hermes Bridge (port 8787)...")
            subprocess.Popen(
                [str(venv_python), str(hermes)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, start_new_session=True,
            )
            time.sleep(3)
            if not check_port(8787):
                ok("Hermes Bridge running on :8787")
                started.append("hermes")
            else:
                warn("Hermes Bridge may still be starting")

    # Dashboard
    dashboard_html = dashboard_dir / "mission-control.html"
    if dashboard_html.exists():
        if not check_port(8000):
            ok("Dashboard already serving on :8000")
            started.append("dashboard")
        else:
            info("Starting Dashboard (port 8000)...")
            subprocess.Popen(
                [sys.executable, "-m", "http.server", "8000"],
                cwd=str(dashboard_dir),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(1)
            if not check_port(8000):
                ok("Dashboard serving at http://localhost:8000")
                started.append("dashboard")
            else:
                warn("Port 8000 may be in use")

    # Voice Bridge (optional)
    voice_venv = bridge_dir / "voice-venv"
    voice_bridge = bridge_dir / "voice_bridge.py"
    if voice_venv.exists() and voice_bridge.exists():
        if not check_port(8686):
            ok("Voice Bridge already running on :8686")
            started.append("voice")
        elif ask_yn("Start Voice Bridge? (local mic, port 8686)", default=False):
            voice_python = voice_venv / "bin" / "python"
            subprocess.Popen(
                [str(voice_python), str(voice_bridge)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, start_new_session=True,
            )
            time.sleep(2)
            if not check_port(8686):
                ok("Voice Bridge running on :8686")
                started.append("voice")
            else:
                warn("Voice Bridge may still be starting")

    if started:
        ok(f"Active services: {', '.join(started)}")
    else:
        info("No services started — use launchers/ scripts to start individually")


# ─── Phase 11: Verification ──────────────────────────────────────────────────

def verify_installation():
    """Run health checks to verify the installation."""
    header("Phase 11: Verification")

    try:
        import httpx
    except ImportError:
        fail("httpx not available — cannot run verification")
        return False

    checks = [
        ("Ollama", "http://localhost:11434/api/tags"),
        ("Qdrant", "http://localhost:6333/collections"),
        ("PostgreSQL (via Docker)", None),
        ("n8n", "http://localhost:5678/healthz"),
    ]

    all_ok = True
    for name, url in checks:
        if url is None:
            # Check Postgres via docker
            result = run("docker exec superclaw-postgres pg_isready", check=False)
            if result is not None:
                ok(f"{name}: accepting connections")
            else:
                warn(f"{name}: not reachable (may still be starting)")
                all_ok = False
            continue

        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code < 500:
                ok(f"{name}: up ({resp.status_code})")
            else:
                warn(f"{name}: degraded ({resp.status_code})")
                all_ok = False
        except Exception:
            warn(f"{name}: not reachable")
            all_ok = False

    # Check embedding model
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        has_embed = any("embed" in m for m in models)
        if has_embed:
            ok("Embedding model available")
        else:
            warn("No embedding model found — run: ollama pull nomic-embed-text")
            all_ok = False
    except Exception:
        pass

    # Bridges (informational — may not be running)
    for name, url in [
        ("Lazarus Bridge", "http://localhost:8888/health"),
        ("Hermes Bridge", "http://localhost:8787/api/v1/health"),
    ]:
        try:
            resp = httpx.get(url, timeout=3)
            ok(f"{name}: running")
        except Exception:
            info(f"{name}: not running")

    # Dashboard
    try:
        resp = httpx.get("http://localhost:8000/", timeout=3)
        if resp.status_code == 200:
            ok("Dashboard: serving on :8000")
        else:
            info("Dashboard: not running")
    except Exception:
        info("Dashboard: not running")

    # Voice Bridge
    try:
        resp = httpx.get("http://localhost:8686/health", timeout=3)
        ok("Voice Bridge: running on :8686")
    except Exception:
        info("Voice Bridge: not running")

    # Check LanceDB directory
    lancedb_dir = MEMORY_DIR / "lancedb"
    if lancedb_dir.exists():
        ok(f"LanceDB directory: {lancedb_dir}")
    else:
        info("LanceDB directory will be created on first memory_store")

    return all_ok


# ─── Phase 12: Summary ───────────────────────────────────────────────────────

def print_summary(all_ok):
    """Print setup summary and next steps."""
    header("Setup Complete")

    if all_ok:
        print(f"""
  {Colors.GREEN}SuperClaw is ready.{Colors.END}

  {Colors.BOLD}Next Step:{Colors.END}
    Start the SuperClaw runtime:
       superclaw

  {Colors.BOLD}Services:{Colors.END}
    SuperClaw:  http://localhost:18789  (start with: superclaw)
    Ollama:     http://localhost:11434
    PostgreSQL: localhost:5432
    Qdrant:     http://localhost:6333
    n8n:        http://localhost:5678
    Lazarus:    http://localhost:8888
    Hermes:     http://localhost:8787
    Dashboard:  http://localhost:8000
    Voice:      http://localhost:8686  (if started)

  {Colors.BOLD}Agent Templates:{Colors.END}
    coordinator, developer, researcher, monitor, analyst
    Customize in: {WORKSPACE_DIR}/agents/

  {Colors.BOLD}Voice & Phone:{Colors.END}
    Local mic:      bridge/voice-venv/bin/python bridge/voice_bridge.py
    Discord voice:  DISCORD_BOT_TOKEN=xxx bridge/voice-venv/bin/python bridge/discord_bridge.py
    Discord text:   superclaw onboard  (select Discord, paste bot token)
    Phone calls:    See docs/VOICE.md + configs/elevenlabs-agent.json

  {Colors.BOLD}Key Paths:{Colors.END}
    Config:     {SUPERCLAW_HOME}/superclaw.json
    Workspace:  {WORKSPACE_DIR}/
    Memory:     {MEMORY_DIR}/lancedb/
    Agents:     {WORKSPACE_DIR}/agents/
    Docs:       {SUPERCLAW_DIR}/docs/

  {Colors.BOLD}Stop Services:{Colors.END}
    Docker:     docker compose -f {SUPERCLAW_DIR}/docker-compose.yml down
    Bridges:    pkill -f lazarus_bridge; pkill -f hermes_bridge
    Dashboard:  pkill -f 'http.server 8000'
    Voice:      pkill -f voice_bridge
""")
    else:
        print(f"""
  {Colors.YELLOW}SuperClaw is partially set up.{Colors.END}

  Some services may not be running yet. Check:
    python3 {SUPERCLAW_DIR}/scripts/healthcheck.py

  Common fixes:
    - Start Ollama:  ollama serve
    - Start Docker:  docker compose -f {SUPERCLAW_DIR}/docker-compose.yml up -d
    - Pull models:   ollama pull nomic-embed-text
    - Start bridges: bash {SUPERCLAW_DIR}/launchers/start-bridges.sh
    - Voice setup:   bash {SUPERCLAW_DIR}/scripts/setup-voice.sh
""")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SuperClaw Setup Wizard")
    parser.add_argument("--check", action="store_true", help="Verify existing installation only")
    parser.add_argument("--minimal", action="store_true", help="Skip optional components")
    args = parser.parse_args()

    print(BANNER)

    if args.check:
        header("Installation Verification")
        all_ok = verify_installation()
        print_summary(all_ok)
        sys.exit(0 if all_ok else 1)

    # Full setup flow
    issues = check_prerequisites()

    if not install_missing(issues):
        print(f"\n  {Colors.RED}Setup cannot continue. Fix the issues above and re-run.{Colors.END}\n")
        sys.exit(1)

    setup_directories()
    setup_environment()
    setup_superclaw_config()

    if not args.minimal:
        setup_ollama()

    setup_docker()
    setup_bridges()

    if not args.minimal:
        setup_voice()

    start_services()
    all_ok = verify_installation()
    print_summary(all_ok)


if __name__ == "__main__":
    main()
