#!/usr/bin/env python3
"""Git-based auto-updater: checks for updates, pulls, and restarts."""

import subprocess
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_git_version() -> str:
    """Build a version string from git metadata."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=SCRIPT_DIR, stderr=subprocess.DEVNULL,
        ).decode().strip()

        try:
            tag = subprocess.check_output(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=SCRIPT_DIR, stderr=subprocess.DEVNULL,
            ).decode().strip()
        except subprocess.CalledProcessError:
            tag = "0.0.0"

        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=SCRIPT_DIR, stderr=subprocess.DEVNULL,
        ).decode().strip()

        return f"{tag}+{commit} ({branch})"
    except Exception:
        version_file = os.path.join(SCRIPT_DIR, "VERSION")
        if os.path.exists(version_file):
            with open(version_file) as f:
                return f.read().strip()
        return "unknown"


def check_for_updates() -> bool:
    """Fetch from origin and return True when local is behind remote."""
    try:
        subprocess.check_call(
            ["git", "fetch"],
            cwd=SCRIPT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        local = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=SCRIPT_DIR,
        ).decode().strip()
        remote = subprocess.check_output(
            ["git", "rev-parse", "@{u}"], cwd=SCRIPT_DIR,
        ).decode().strip()
        return local != remote
    except Exception as exc:
        print(f"Update check failed: {exc}")
        return False


def pull_latest() -> bool:
    """Fast-forward pull. Returns True on success."""
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=SCRIPT_DIR,
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"✅ Git pull: {result.stdout.strip()}")
            return True
        print(f"❌ Git pull failed: {result.stderr.strip()}")
        return False
    except Exception as exc:
        print(f"❌ Git pull exception: {exc}")
        return False


def install_requirements():
    """Run pip install -r requirements.txt (idempotent)."""
    req_file = os.path.join(SCRIPT_DIR, "requirements.txt")
    if not os.path.exists(req_file):
        return
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req_file],
            cwd=SCRIPT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("✅ pip requirements satisfied")
    except subprocess.CalledProcessError as exc:
        print(f"⚠️  pip install failed: {exc}")


def restart_process():
    """Replace the current process with a fresh invocation of the same script."""
    print("🔄 Restarting process …")
    time.sleep(0.5)
    os.execv(sys.executable, [sys.executable] + sys.argv)