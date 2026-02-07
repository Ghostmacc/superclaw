#!/usr/bin/env python3
"""
SuperClaw Vault — Secret Management

Encrypts secrets at rest and generates gateway config on startup.
Secrets are stored in an encrypted vault file, not in plaintext configs.

Flow:
  1. `vault.py init`     — Create vault + config template from existing config
  2. `vault.py unlock`   — Decrypt vault → generate live config → start gateway
  3. `vault.py rotate`   — Change vault passphrase
  4. `vault.py set KEY`  — Update a single secret
  5. `vault.py list`     — Show secret names (not values)

The vault uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
The passphrase is never stored — you enter it on unlock.

Usage:
  # First time: extract secrets from existing config into vault
  python3 vault.py init

  # On startup: decrypt secrets and generate live config
  python3 vault.py unlock

  # Update a secret
  python3 vault.py set OPENROUTER_API_KEY

  # Rotate passphrase
  python3 vault.py rotate
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Missing dependency: pip install cryptography")
    sys.exit(1)

# ── Paths ────────────────────────────────────────────────────────────────

SUPERCLAW_HOME = Path(os.environ.get("SUPERCLAW_HOME", Path.home() / ".superclaw"))
if not SUPERCLAW_HOME.exists():
    SUPERCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))

VAULT_FILE = SUPERCLAW_HOME / ".vault.enc"
TEMPLATE_FILE = SUPERCLAW_HOME / "config.template.json"
LIVE_CONFIG = SUPERCLAW_HOME / "superclaw.json"
# Fallback for existing deployments
if not LIVE_CONFIG.exists() and (SUPERCLAW_HOME / "openclaw.json").exists():
    LIVE_CONFIG = SUPERCLAW_HOME / "openclaw.json"

# Secret patterns to detect in config
SECRET_KEYS = [
    "OPENROUTER_API_KEY",
    "apiKey",
    "token",
    "botToken",
    "secret",
    "password",
]


# ── Crypto ───────────────────────────────────────────────────────────────

def derive_key(passphrase: str) -> bytes:
    """Derive a Fernet key from a passphrase using PBKDF2."""
    # Use a fixed salt derived from the vault path (unique per install)
    salt = hashlib.sha256(str(VAULT_FILE).encode()).digest()[:16]
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 100_000)
    return base64.urlsafe_b64encode(key[:32])


def encrypt_vault(secrets: dict, passphrase: str) -> None:
    """Encrypt secrets dict and write to vault file."""
    key = derive_key(passphrase)
    f = Fernet(key)
    data = json.dumps(secrets, indent=2).encode()
    encrypted = f.encrypt(data)
    VAULT_FILE.write_bytes(encrypted)
    os.chmod(VAULT_FILE, 0o600)


def decrypt_vault(passphrase: str) -> dict:
    """Read and decrypt vault file. Returns secrets dict."""
    if not VAULT_FILE.exists():
        print(f"No vault found at {VAULT_FILE}. Run: vault.py init")
        sys.exit(1)
    key = derive_key(passphrase)
    f = Fernet(key)
    try:
        decrypted = f.decrypt(VAULT_FILE.read_bytes())
        return json.loads(decrypted)
    except Exception:
        print("Wrong passphrase or corrupted vault.")
        sys.exit(1)


# ── Secret Detection ─────────────────────────────────────────────────────

def find_secrets_in_config(config: dict, path="") -> dict:
    """
    Walk a config dict and find values that look like secrets.
    Returns {placeholder_name: actual_value}.
    """
    secrets = {}
    if isinstance(config, dict):
        for k, v in config.items():
            current_path = f"{path}.{k}" if path else k
            if isinstance(v, str) and _is_secret_key(k, v):
                placeholder = _make_placeholder(current_path)
                secrets[placeholder] = v
            elif isinstance(v, (dict, list)):
                secrets.update(find_secrets_in_config(v, current_path))
    elif isinstance(config, list):
        for i, item in enumerate(config):
            secrets.update(find_secrets_in_config(item, f"{path}[{i}]"))
    return secrets


def _is_secret_key(key: str, value: str) -> bool:
    """Check if a key/value pair looks like a secret."""
    key_lower = key.lower()
    # Check key name patterns
    for pattern in ["key", "token", "secret", "password", "credential", "auth"]:
        if pattern in key_lower:
            # Exclude placeholder values and short values
            if value and len(value) > 8 and not value.startswith("${") and not value.startswith("GENERATE"):
                return True
    return False


def _make_placeholder(path: str) -> str:
    """Convert a JSON path to a placeholder name."""
    # env.OPENROUTER_API_KEY → OPENROUTER_API_KEY
    # gateway.auth.token → GATEWAY_AUTH_TOKEN
    parts = path.replace("[", ".").replace("]", "").split(".")
    # Use the last meaningful parts
    clean = "_".join(p.upper() for p in parts if p and not p.isdigit())
    return clean


def replace_secrets_with_placeholders(config: dict, secrets: dict, path="") -> dict:
    """Replace secret values in config with ${PLACEHOLDER} markers."""
    if isinstance(config, dict):
        result = {}
        for k, v in config.items():
            current_path = f"{path}.{k}" if path else k
            if isinstance(v, str):
                placeholder = _make_placeholder(current_path)
                if placeholder in secrets and secrets[placeholder] == v:
                    result[k] = f"${{{placeholder}}}"
                else:
                    result[k] = v
            elif isinstance(v, (dict, list)):
                result[k] = replace_secrets_with_placeholders(v, secrets, current_path)
            else:
                result[k] = v
        return result
    elif isinstance(config, list):
        return [
            replace_secrets_with_placeholders(item, secrets, f"{path}[{i}]")
            for i, item in enumerate(config)
        ]
    return config


def inject_secrets(template: dict, secrets: dict) -> dict:
    """Replace ${PLACEHOLDER} markers in template with actual secret values."""
    text = json.dumps(template)
    for placeholder, value in secrets.items():
        text = text.replace(f"${{{placeholder}}}", value)
    return json.loads(text)


# ── Commands ─────────────────────────────────────────────────────────────

def cmd_init():
    """Extract secrets from existing config into encrypted vault."""
    if not LIVE_CONFIG.exists():
        print(f"No config found at {LIVE_CONFIG}")
        print("Create your config first, then run vault.py init to secure it.")
        sys.exit(1)

    config = json.loads(LIVE_CONFIG.read_text())
    secrets = find_secrets_in_config(config)

    if not secrets:
        print("No secrets detected in config. Nothing to vault.")
        return

    print(f"Found {len(secrets)} secret(s):")
    for name in sorted(secrets):
        masked = secrets[name][:4] + "..." + secrets[name][-4:] if len(secrets[name]) > 12 else "****"
        print(f"  {name}: {masked}")

    # Get passphrase
    print()
    passphrase = getpass.getpass("Create vault passphrase: ")
    confirm = getpass.getpass("Confirm passphrase: ")
    if passphrase != confirm:
        print("Passphrases don't match.")
        sys.exit(1)
    if len(passphrase) < 8:
        print("Passphrase must be at least 8 characters.")
        sys.exit(1)

    # Create vault
    encrypt_vault(secrets, passphrase)
    print(f"Vault created: {VAULT_FILE}")

    # Create template (config with placeholders instead of secrets)
    template = replace_secrets_with_placeholders(config, secrets)
    TEMPLATE_FILE.write_text(json.dumps(template, indent=2))
    os.chmod(TEMPLATE_FILE, 0o600)
    print(f"Template created: {TEMPLATE_FILE}")

    print()
    print("Your secrets are now encrypted. On startup, run:")
    print("  python3 vault.py unlock")
    print()
    print("This will decrypt your secrets and regenerate the live config.")


def cmd_unlock():
    """Decrypt vault and generate live config."""
    if not TEMPLATE_FILE.exists():
        print(f"No template found at {TEMPLATE_FILE}. Run: vault.py init")
        sys.exit(1)

    passphrase = getpass.getpass("Vault passphrase: ")
    secrets = decrypt_vault(passphrase)

    template = json.loads(TEMPLATE_FILE.read_text())
    config = inject_secrets(template, secrets)

    LIVE_CONFIG.write_text(json.dumps(config, indent=2))
    os.chmod(LIVE_CONFIG, 0o600)
    print(f"Config generated: {LIVE_CONFIG}")
    print(f"Secrets injected: {len(secrets)}")


def cmd_set(key_name):
    """Update a single secret in the vault."""
    passphrase = getpass.getpass("Vault passphrase: ")
    secrets = decrypt_vault(passphrase)

    if key_name not in secrets:
        print(f"Available secrets: {', '.join(sorted(secrets.keys()))}")
        create = input(f"'{key_name}' not found. Create it? [y/N]: ").strip().lower()
        if create != "y":
            return

    new_value = getpass.getpass(f"New value for {key_name}: ")
    if not new_value:
        print("Empty value. Aborting.")
        return

    secrets[key_name] = new_value
    encrypt_vault(secrets, passphrase)
    print(f"Updated: {key_name}")

    # Also update template if needed
    if TEMPLATE_FILE.exists():
        template = json.loads(TEMPLATE_FILE.read_text())
        template_text = json.dumps(template)
        if f"${{{key_name}}}" not in template_text:
            print(f"Note: ${{{key_name}}} not found in template. You may need to add it manually.")


def cmd_rotate():
    """Change vault passphrase."""
    old_pass = getpass.getpass("Current passphrase: ")
    secrets = decrypt_vault(old_pass)

    new_pass = getpass.getpass("New passphrase: ")
    confirm = getpass.getpass("Confirm new passphrase: ")
    if new_pass != confirm:
        print("Passphrases don't match.")
        sys.exit(1)

    encrypt_vault(secrets, new_pass)
    print("Passphrase rotated successfully.")


def cmd_list():
    """Show secret names (not values)."""
    passphrase = getpass.getpass("Vault passphrase: ")
    secrets = decrypt_vault(passphrase)

    print(f"\nVault contains {len(secrets)} secret(s):")
    for name in sorted(secrets):
        val = secrets[name]
        masked = val[:4] + "..." + val[-4:] if len(val) > 12 else "****"
        print(f"  {name}: {masked}")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SuperClaw Vault — Encrypted Secret Management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Flow:
  1. vault.py init      Extract secrets from config → encrypted vault
  2. vault.py unlock    Decrypt vault → regenerate live config
  3. vault.py set KEY   Update a secret
  4. vault.py rotate    Change vault passphrase
  5. vault.py list      Show secret names (masked)

Secrets are encrypted with AES-128-CBC + HMAC-SHA256 (Fernet).
The passphrase is never stored — enter it on each unlock.
        """,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Extract secrets from existing config into vault")
    sub.add_parser("unlock", help="Decrypt vault and generate live config")

    p_set = sub.add_parser("set", help="Update a single secret")
    p_set.add_argument("key", help="Secret name to update")

    sub.add_parser("rotate", help="Change vault passphrase")
    sub.add_parser("list", help="Show secret names (masked)")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "unlock":
        cmd_unlock()
    elif args.command == "set":
        cmd_set(args.key)
    elif args.command == "rotate":
        cmd_rotate()
    elif args.command == "list":
        cmd_list()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
