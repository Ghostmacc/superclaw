#!/usr/bin/env python3
"""
SkillGuard — SuperClaw Skill Security Pipeline

Downloads, isolates, scans, renames, and installs ClawHub skills safely.

Pipeline:
  1. Isolate skill in quarantine directory
  2. Scan with Cisco skill-scanner (static + behavioral analysis)
  3. If clean: rename openclaw→superclaw references
  4. Install to SuperClaw workspace
  5. Log results to scan registry

Requires: pip install cisco-ai-skill-scanner

Usage:
  python3 skill-guard.py scan /path/to/skill           # Scan a skill
  python3 skill-guard.py scan /path/to/skill --rename   # Scan + rename if clean
  python3 skill-guard.py scan-all                       # Scan all installed skills
  python3 skill-guard.py install /path/to/skill         # Full pipeline: quarantine→scan→rename→install
  python3 skill-guard.py cron                           # Re-scan all (for scheduled runs)
  python3 skill-guard.py history                        # Show scan log
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────

SUPERCLAW_HOME = Path(os.environ.get("SUPERCLAW_HOME", Path.home() / ".superclaw"))
# Fall back to .openclaw if .superclaw doesn't exist yet
if not SUPERCLAW_HOME.exists():
    SUPERCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))

WORKSPACE = SUPERCLAW_HOME / "workspace"
SKILLS_DIR = WORKSPACE / "skills"
QUARANTINE_DIR = WORKSPACE / "quarantine"
SCAN_REGISTRY = WORKSPACE / "memory" / "skill_scans.json"

# ── Rename Mappings ──────────────────────────────────────────────────────

RENAME_PATTERNS = [
    (r"~/\.openclaw/", "~/.superclaw/"),
    (r"\.openclaw/", ".superclaw/"),
    (r"openclaw\.json", "superclaw.json"),
    (r"OPENCLAW_", "SUPERCLAW_"),
]

# File extensions to process for renames
TEXT_EXTENSIONS = {
    ".md", ".json", ".js", ".mjs", ".ts", ".py", ".sh", ".bash",
    ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini", ".env",
}


# ── Utilities ────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    symbol = {"OK": "+", "WARN": "!", "ERROR": "x", "INFO": "*"}
    print(f"[{ts}] [{symbol.get(level, '*')}] {msg}")


def ensure_dirs():
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    SCAN_REGISTRY.parent.mkdir(parents=True, exist_ok=True)


def load_registry():
    if SCAN_REGISTRY.exists():
        try:
            return json.loads(SCAN_REGISTRY.read_text())
        except json.JSONDecodeError:
            return {"scans": [], "version": 1}
    return {"scans": [], "version": 1}


def save_registry(registry):
    tmp = SCAN_REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2))
    tmp.replace(SCAN_REGISTRY)


# ── Scanner ──────────────────────────────────────────────────────────────

def run_scanner(skill_path, use_behavioral=True):
    """
    Run Cisco skill-scanner on a skill directory.
    Returns (clean: bool, findings: list, raw_output: str).
    """
    cmd = ["skill-scanner", "scan", str(skill_path), "--format", "json"]
    if use_behavioral:
        cmd.append("--use-behavioral")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout.strip()

        try:
            data = json.loads(output)
            finding_list = data if isinstance(data, list) else data.get("findings", [])
            has_critical = any(
                f.get("severity", "").lower() in ("critical", "high")
                for f in finding_list
            )
            return (not has_critical, finding_list, output)
        except json.JSONDecodeError:
            # Fall back to exit code
            return (result.returncode == 0, [], output)

    except FileNotFoundError:
        log("skill-scanner not installed. Run: pip install cisco-ai-skill-scanner", "ERROR")
        return (False, [], "skill-scanner binary not found")
    except subprocess.TimeoutExpired:
        log("Scan timed out after 120s", "ERROR")
        return (False, [], "timeout")


# ── Renamer ──────────────────────────────────────────────────────────────

def rename_openclaw_refs(skill_path):
    """Rename all openclaw references to superclaw in text files."""
    renamed_files = []
    skill_path = Path(skill_path)

    for fpath in skill_path.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        # Skip hidden dirs like .git
        if any(part.startswith(".") for part in fpath.relative_to(skill_path).parts[:-1]):
            continue

        try:
            content = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue

        original = content
        for pattern, replacement in RENAME_PATTERNS:
            content = re.sub(re.escape(pattern) if not pattern.startswith(r"~") else pattern, replacement, content)

        if content != original:
            fpath.write_text(content, encoding="utf-8")
            rel = str(fpath.relative_to(skill_path))
            renamed_files.append(rel)
            log(f"  Renamed refs in: {rel}")

    return renamed_files


# ── Commands ─────────────────────────────────────────────────────────────

def scan_skill(skill_path, rename=False):
    """Scan a single skill. Returns scan result dict."""
    skill_path = Path(skill_path).resolve()
    skill_name = skill_path.name

    log(f"Scanning: {skill_name}")

    clean, findings, raw = run_scanner(skill_path)

    result = {
        "skill": skill_name,
        "path": str(skill_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "clean": clean,
        "findings_count": len(findings),
        "findings_summary": [
            {"severity": f.get("severity", "?"), "message": f.get("description", f.get("message", "?"))[:200]}
            for f in findings[:10]
        ],
        "renamed": False,
        "renamed_files": [],
    }

    if clean:
        log(f"CLEAN: {skill_name}", "OK")
        if rename:
            renamed = rename_openclaw_refs(skill_path)
            result["renamed"] = bool(renamed)
            result["renamed_files"] = renamed
            if renamed:
                log(f"  Renamed {len(renamed)} file(s)")
            else:
                log(f"  No openclaw refs to rename")
    else:
        log(f"THREAT DETECTED: {skill_name} — {len(findings)} finding(s)", "WARN")
        for f in findings[:5]:
            desc = f.get("description", f.get("message", "unknown"))[:120]
            sev = f.get("severity", "?")
            log(f"  [{sev}] {desc}", "WARN")

    return result


def scan_all(rename=False):
    """Scan all installed skills."""
    if not SKILLS_DIR.exists():
        log(f"No skills directory at {SKILLS_DIR}", "ERROR")
        return []

    results = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if skill_dir.is_dir() and not skill_dir.name.startswith("."):
            result = scan_skill(skill_dir, rename=rename)
            results.append(result)

    return results


def install_skill(skill_source, skip_scan=False):
    """Full pipeline: quarantine → scan → rename → install."""
    source = Path(skill_source).resolve()
    if not source.exists():
        log(f"Skill source not found: {source}", "ERROR")
        return False

    skill_name = source.name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine_path = QUARANTINE_DIR / f"{skill_name}_{ts}"

    # Step 1: Quarantine
    log(f"Step 1/4: Quarantining → {quarantine_path}")
    shutil.copytree(source, quarantine_path)

    # Step 2: Scan
    if not skip_scan:
        log("Step 2/4: Scanning with skill-scanner")
        result = scan_skill(quarantine_path, rename=False)

        registry = load_registry()
        registry["scans"].append(result)
        save_registry(registry)

        if not result["clean"]:
            log(f"BLOCKED: {skill_name} failed scan. Quarantined at: {quarantine_path}", "ERROR")
            log("Review findings with: python3 skill-guard.py history", "ERROR")
            return False
    else:
        log("Step 2/4: SKIPPED (--skip-scan)", "WARN")

    # Step 3: Rename
    log("Step 3/4: Renaming openclaw → superclaw")
    renamed = rename_openclaw_refs(quarantine_path)
    if renamed:
        log(f"  Renamed {len(renamed)} file(s)")
    else:
        log("  No references needed renaming")

    # Step 4: Install
    install_path = SKILLS_DIR / skill_name
    if install_path.exists():
        backup = SKILLS_DIR / f"{skill_name}.bak.{ts}"
        log(f"Step 4/4: Backing up existing → {backup.name}")
        shutil.move(str(install_path), str(backup))
    else:
        log(f"Step 4/4: Installing → {install_path}")

    shutil.copytree(quarantine_path, install_path)
    log(f"INSTALLED: {skill_name}", "OK")

    # Update registry
    registry = load_registry()
    registry["scans"].append({
        "skill": skill_name,
        "path": str(install_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "clean": True,
        "renamed": bool(renamed),
        "renamed_files": renamed,
        "installed": True,
    })
    save_registry(registry)

    # Cleanup quarantine
    shutil.rmtree(quarantine_path, ignore_errors=True)
    log("Quarantine cleaned")

    return True


def cron_mode():
    """Re-scan all installed skills. Exit 1 if threats found."""
    log("═══ SkillGuard Cron Scan ═══")
    results = scan_all(rename=False)

    registry = load_registry()
    for r in results:
        registry["scans"].append(r)
    save_registry(registry)

    clean = sum(1 for r in results if r["clean"])
    total = len(results)
    log(f"Scan complete: {clean}/{total} skills clean")

    if clean < total:
        threats = [r["skill"] for r in results if not r["clean"]]
        log(f"THREATS: {', '.join(threats)}", "WARN")
        return 1
    return 0


def show_history():
    """Display scan history."""
    registry = load_registry()
    scans = registry.get("scans", [])

    if not scans:
        print("No scan history. Run: python3 skill-guard.py scan-all")
        return

    print(f"\n{'Skill':<20} {'Date':<22} {'Status':<10} {'Renamed':<10} {'Findings'}")
    print("─" * 80)
    for s in scans[-25:]:
        status = "CLEAN" if s.get("clean") else "THREAT"
        renamed = "yes" if s.get("renamed") else "no"
        installed = " [installed]" if s.get("installed") else ""
        ts = s.get("timestamp", "?")[:19]
        findings = s.get("findings_count", 0)
        print(f"{s['skill']:<20} {ts:<22} {status:<10} {renamed:<10} {findings}{installed}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SkillGuard — SuperClaw Skill Security Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Pipeline: quarantine → scan → rename → install

Examples:
  %(prog)s scan ./my-skill              Scan a skill directory
  %(prog)s scan ./my-skill --rename     Scan + rename openclaw refs if clean
  %(prog)s scan-all                     Scan all installed skills
  %(prog)s install ./downloaded-skill   Full pipeline (quarantine→scan→rename→install)
  %(prog)s cron                         Re-scan all installed (for cron jobs)
  %(prog)s history                      View scan log

Requires: pip install cisco-ai-skill-scanner
        """,
    )
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Scan a skill directory")
    p_scan.add_argument("path", help="Path to skill directory")
    p_scan.add_argument("--rename", action="store_true", help="Rename openclaw→superclaw if clean")

    p_all = sub.add_parser("scan-all", help="Scan all installed skills")
    p_all.add_argument("--rename", action="store_true", help="Rename clean skills")

    p_inst = sub.add_parser("install", help="Full pipeline: quarantine → scan → rename → install")
    p_inst.add_argument("path", help="Path to skill source directory")
    p_inst.add_argument("--skip-scan", action="store_true", help="Skip security scan (NOT recommended)")

    sub.add_parser("cron", help="Re-scan all installed skills (for cron jobs)")
    sub.add_parser("history", help="Show scan history")

    args = parser.parse_args()
    ensure_dirs()

    if args.command == "scan":
        result = scan_skill(args.path, rename=args.rename)
        registry = load_registry()
        registry["scans"].append(result)
        save_registry(registry)
        sys.exit(0 if result["clean"] else 1)

    elif args.command == "scan-all":
        results = scan_all(rename=args.rename)
        registry = load_registry()
        registry["scans"].extend(results)
        save_registry(registry)
        threats = [r for r in results if not r["clean"]]
        sys.exit(1 if threats else 0)

    elif args.command == "install":
        ok = install_skill(args.path, skip_scan=args.skip_scan)
        sys.exit(0 if ok else 1)

    elif args.command == "cron":
        sys.exit(cron_mode())

    elif args.command == "history":
        show_history()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
