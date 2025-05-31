"""
manage_actions_runners.py

Automatically start or stop Dockerised self‑hosted GitHub Actions runners on
a per‑repository basis.

* Looks at all repositories under $GH_OWNER (user or organisation)
* If a repo has had commits within ABANDON_DAYS (default 7) it ensures a
  runner container is running for it.
* If a repo has been inactive for longer, the matching container is removed.

Requirements
------------
* gh CLI signed‑in with a Personal Access Token (PAT) via $GH_TOKEN
* Docker daemon available on the host
* The official runner container image (ghcr.io/actions/runner:latest) pulled
  automatically when a new runner starts
* Python 3.10+

Environment variables used
-------------------------
GH_OWNER        – User or org whose repos are managed (required)
ABANDON_DAYS    – Cut‑off in days for “activity” (default 7)
RUNNER_IMAGE    – Container image to use (default ghcr.io/actions/runner:latest)

Example crontab (run hourly)
---------------------------
0 * * * * /usr/bin/env bash -c "/usr/bin/python3 /opt/gha/manage_actions_runners.py >>/var/log/manage_runners.log 2>&1"
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict

# ---------------------------------------------------------------------------
# Configuration via env vars -------------------------------------------------
# ---------------------------------------------------------------------------

GH_OWNER = os.getenv("GH_OWNER")  # e.g. "my‑username"  (required)
ABANDON_THRESHOLD = timedelta(days=int(os.getenv("ABANDON_DAYS", "7")))
RUNNER_IMAGE = os.getenv("RUNNER_IMAGE", "ghcr.io/actions/runner:latest")

# ---------------------------------------------------------------------------
# Helper utilities -----------------------------------------------------------
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    """Return an aware UTC datetime representing *now*."""
    return datetime.now(timezone.utc)


def run(cmd: List[str], capture: bool = True) -> str:
    """Execute *cmd* returning stdout (str). Raises if exit status != 0."""
    logging.debug("$ %s", " ".join(cmd))
    res = subprocess.run(cmd, text=True, capture_output=capture, check=True)
    return res.stdout if capture else ""

# ---------------------------------------------------------------------------
# GitHub helpers -------------------------------------------------------------
# ---------------------------------------------------------------------------

def list_repos() -> List[Dict]:
    """Return [{'name': 'foo', 'updatedAt': '2025-05-30T17:02:43Z'}, ...]."""
    if not GH_OWNER:
        sys.exit("Environment variable GH_OWNER must be set – exiting.")

    output = run([
        "gh", "repo", "list", GH_OWNER,
        "--json", "name,updatedAt",
        "--limit", "1000",
    ])
    return json.loads(output)


def get_registration_token(repo: str) -> str:
    """Fetch a short‑lived registration token for *repo*."""
    token = run([
        "gh", "api", "-X", "POST",
        f"/repos/{repo}/actions/runners/registration-token",
        "--jq", ".token",
    ]).strip()
    return token

# ---------------------------------------------------------------------------
# Docker helpers -------------------------------------------------------------
# ---------------------------------------------------------------------------

def container_name(repo: str) -> str:
    return f"gha_runner_{repo.replace('/', '_')}"


def container_exists(name: str) -> bool:
    return bool(run(["docker", "ps", "-a", "-q", "-f", f"name=^{name}$"]).strip())


def container_running(name: str) -> bool:
    return bool(run(["docker", "ps", "-q", "-f", f"name=^{name}$"]).strip())


def start_container(repo: str) -> None:
    name = container_name(repo)
    if container_running(name):
        logging.info("Runner for %s already running", repo)
        return

    token = get_registration_token(repo)
    logging.info("Starting runner container for %s", repo)

    run([
        "docker", "run", "-d", "--restart", "always",
        "--name", name,
        "-e", f"RUNNER_NAME={name}",
        "-e", f"RUNNER_REPOSITORY={repo}",
        "-e", f"RUNNER_TOKEN={token}",
        "-e", "RUNNER_LABELS=self-hosted,docker",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        RUNNER_IMAGE,
    ], capture=False)


def stop_container(repo: str) -> None:
    name = container_name(repo)
    if not container_exists(name):
        return
    logging.info("Stopping and removing container for %s", repo)
    run(["docker", "rm", "-f", name], capture=False)

# ---------------------------------------------------------------------------
# Main logic -----------------------------------------------------------------
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    repos = list_repos()
    cutoff = utcnow() - ABANDON_THRESHOLD

    active = {
        r["name"]
        for r in repos
        if datetime.fromisoformat(r["updatedAt"].rstrip("Z")).replace(tzinfo=timezone.utc) >= cutoff
    }

    logging.info("Active repos since %s: %s", cutoff.date(), ", ".join(sorted(active)))

    # Ensure runners for active repositories
    for repo in active:
        start_container(f"{GH_OWNER}/{repo}")

    # Remove runners belonging to inactive repositories
    containers = run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture=True).splitlines()
    for name in (c for c in containers if c.startswith("gha_runner_")):
        repo = name.removeprefix("gha_runner_").replace("_", "/")
        if repo not in active:
            stop_container(f"{GH_OWNER}/{repo}")


if __name__ == "__main__":
    main()

