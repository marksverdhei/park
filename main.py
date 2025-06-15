from __future__ import annotations

"""A small helper that automatically maintains one ephemeral GitHub
self‑hosted runner **per active repository**.

* **Active** = the repo had **either**  
  • a commit **or**  
  • a workflow run  
  during the last 7 days.
* The repo must also contain **at least one workflow file** in
  `.github/workflows/` whose YAML contains the string `self-hosted`.

The script relies on the `gh` CLI and Docker being installed and
authenticated.

Run it on a cadence (e.g. every 30 min) from a small server or CI job.

### Debugging
Logging is now handled via `logging` and is **very chatty** in `DEBUG`
mode.  Set an environment variable to control verbosity:

```
export LOG_LEVEL=DEBUG   # or INFO / WARNING / ERROR
```
"""

###############################################################################
# Standard library & third‑party imports
###############################################################################

import base64
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List

import docker
from docker.models.containers import Container
import logging

###############################################################################
# Logging configuration – driven by $LOG_LEVEL (default INFO)
###############################################################################

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__)
logger.debug("Log level set to %s", LOG_LEVEL)

###############################################################################
# Constants & globals
###############################################################################

ACTIVE_THRESHOLD = timedelta(days=7)

docker_client = docker.from_env()
logger.debug("Docker client initialised: %s", docker_client)

###############################################################################
# Helper functions – GitHub CLI wrapper
###############################################################################

def _run_gh(*args: str, **kwargs) -> str:
    """Run *gh* with *args* and return **stdout** (stripped).

    The function logs **everything**:
    * DEBUG – the full command before execution.
    * ERROR – on non‑zero exit, includes command, exit code, *and* the first 300
      characters of both stdout and stderr so you can see the JSON/body that
      came back (which often contains the GitHub REST error message).

    Raises the original `subprocess.CalledProcessError` so callers can decide
    how to react.
    """

    cmd = ["gh", *args]
    cmd_str = " ".join(cmd)
    logger.debug("Running gh: %s", cmd_str)

    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, check=True, **kwargs
        )
    except subprocess.CalledProcessError as exc:
        stdout_snip = (exc.stdout or "").replace("
", "⏎")[:300]
        stderr_snip = (exc.stderr or "").replace("
", "⏎")[:300]
        logger.error(
            "gh FAILED (%s): %s
STDOUT: %s
STDERR: %s",
            exc.returncode,
            cmd_str,
            stdout_snip or "<empty>",
            stderr_snip or "<empty>",
        )
        raise

    out = completed.stdout.strip()
    logger.debug("gh succeeded – output (truncated): %.200s", out.replace("
", "⏎"))
    return out


def _iso_to_dt(value: str) -> datetime:
    """Translate ISO‑8601 strings (possibly ending in `Z`) into tz‑aware dt."""

    value = value.rstrip("Z")
    if value[-1] in ["+", "-"] and ":" in value[-3:]:
        return datetime.fromisoformat(value)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _is_recent(ts: datetime) -> bool:
    return datetime.now(timezone.utc) - ts <= ACTIVE_THRESHOLD

###############################################################################
# GitHub data gathering
###############################################################################

def get_gh_username() -> str:
    user = _run_gh("api", "user", "--jq", ".login")
    logger.info("Authenticated as %s", user)
    return user


def _latest_commit_date(owner: str, repo: str) -> datetime | None:
    try:
        iso_date = _run_gh(
            "api",
            f"repos/{owner}/{repo}/commits",
            "--jq",
            "'.[0].commit.committer.date'",
            "--paginate",
            "--silent",
            "--method",
            "GET",
            "--field",
            "per_page=1",
        )
    except subprocess.CalledProcessError:
        return None
    return _iso_to_dt(iso_date) if iso_date else None


def _latest_workflow_run_date(owner: str, repo: str) -> datetime | None:
    try:
        iso_date = _run_gh(
            "api",
            f"repos/{owner}/{repo}/actions/runs",
            "--jq",
            "'.workflow_runs[0].updated_at'",
            "--field",
            "per_page=1",
        )
    except subprocess.CalledProcessError:
        return None
    return _iso_to_dt(iso_date) if iso_date else None


def _has_self_hosted_workflow(owner: str, repo: str) -> bool:
    try:
        raw = _run_gh(
            "api", f"repos/{owner}/{repo}/contents/.github/workflows", "--jq", "."
        )
    except subprocess.CalledProcessError:
        logger.debug("%s/%s ➜ no workflows directory", owner, repo)
        return False

    try:
        files = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s/%s ➜ Could not parse workflow listing", owner, repo)
        return False

    for item in files:
        if item.get("type") != "file":
            continue
        path = item.get("path")
        if not path or not Path(path).suffix.lower() in {".yml", ".yaml"}:
            continue
        try:
            payload_raw = _run_gh("api", f"repos/{owner}/{repo}/contents/{path}")
            payload = json.loads(payload_raw)
            content_b64 = payload.get("content", "")
            content: str = base64.b64decode(content_b64).decode()
        except Exception as exc:
            logger.debug("%s/%s ➜ failed to fetch %s: %s", owner, repo, path, exc)
            continue
        if "self-hosted" in content:
            logger.debug("%s/%s ➜ workflow %s contains self-hosted", owner, repo, path)
            return True
    logger.debug("%s/%s ➜ no self‑hosted reference found", owner, repo)
    return False

###############################################################################
# Determine which repos need a runner
###############################################################################

def get_active_repos(owner: str) -> List[str]:
    logger.info("Discovering repositories for %s …", owner)
    raw = _run_gh(
        "repo",
        "list",
        owner,
        "--json",
        "name,nameWithOwner",
        "--limit",
        "1000",
    )
    repo_infos = json.loads(raw)

    active: list[str] = []
    for info in repo_infos:
        full = info["nameWithOwner"]  # "owner/repo"
        owner_, repo = full.split("/", 1)
        logger.debug("Evaluating %s/%s", owner_, repo)

        if not _has_self_hosted_workflow(owner_, repo):
            continue

        commit_dt = _latest_commit_date(owner_, repo)
        run_dt = _latest_workflow_run_date(owner_, repo)
        logger.debug(
            "%s/%s commit=%s run=%s",
            owner_,
            repo,
            commit_dt,
            run_dt,
        )

        if (commit_dt and _is_recent(commit_dt)) or (run_dt and _is_recent(run_dt)):
            active.append(repo)
            logger.info("%s/%s marked active", owner_, repo)

    logger.info("Active repos needing runners: %s", active)
    return active

###############################################################################
# Docker helpers (container lifecycle)
###############################################################################

def get_active_runners() -> List[str]:
    containers = docker_client.containers.list()
    runners = []
    for container in containers:
        if container.name.startswith("actions-"):
            parts = container.name.split("-", 2)
            if len(parts) == 3:
                _, _, repo_part = parts
                runners.append(repo_part)
                logger.debug("Found running container %s ➜ repo %s", container.name, repo_part)
    logger.info("Currently running runners: %s", runners)
    return runners

###############################################################################
# Runner spin‑up / tear‑down
###############################################################################

def _get_reg_token(owner: str, repo: str) -> str:
    logger.debug("Requesting registration token for %s/%s", owner, repo)
    out = _run_gh(
        "api",
        "-X",
        "POST",
        f"/repos/{owner}/{repo}/actions/runners/registration-token",
    )
    token = json.loads(out)["token"]
    logger.debug("Received token (length=%d) for %s/%s", len(token), owner, repo)
    return token


def spin_down_runner(owner: str, repo: str) -> None:
    name = f"actions-{owner}-{repo}"
    try:
        container = docker_client.containers.get(name)
    except docker.errors.NotFound:
        logger.warning("Container %s not found – already stopped?", name)
        return
    logger.info("Stopping runner container for %s/%s", owner, repo)
    container.stop()


def spin_up_runner(owner: str, repo: str) -> Container:
    token = _get_reg_token(owner, repo)
    name = f"actions-{owner}-{repo}"
    url = f"https://github.com/{owner}/{repo}"

    logger.info("Starting runner container for %s/%s", owner, repo)
        try:
        container = docker_client.containers.run(
            image="ghcr.io/actions/actions-runner:latest",
            name=name,
            remove=True,
            detach=True,
            environment={"REG_TOKEN": token},
            command=(
                # NOTE: f-string so the {url} placeholder is expanded ✨
                f"sh -c './config.sh --url {url} --token $REG_TOKEN --labels self-hosted && "
                "./run.sh'"
            ),
        )
    except docker.errors.DockerException as exc:
        logger.error("Failed to start container %s: %s", name, exc)
        raise

    logger.debug("Container %s started: %s", name, container.short_id)
    return container

###############################################################################
# Orchestration
###############################################################################

def update_runners(owner: str, desired_repos: Iterable[str]) -> None:
    current = set(get_active_runners())
    desired = set(desired_repos)

    to_stop = current - desired
    to_start = desired - current

    logger.info("Runners to stop: %s", sorted(to_stop))
    logger.info("Runners to start: %s", sorted(to_start))

    for repo in sorted(to_stop):
        spin_down_runner(owner, repo)
    for repo in sorted(to_start):
        spin_up_runner(owner, repo)

###############################################################################
# Entry point
###############################################################################

def main() -> None:
    owner = get_gh_username()
    active_repos = get_active_repos(owner)
    update_runners(owner, active_repos)


if __name__ == "__main__":
    main()
