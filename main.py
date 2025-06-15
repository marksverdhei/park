from __future__ import annotations

"""A small helper that automatically maintains one ephemeral GitHub
self‑hosted runner **per active repository**.

* **Active** = the repo had **either**
  * a commit **or**
  * a workflow run
  during the last 7 days.
* The repo must also contain **at least one workflow file** in
  `.github/workflows/` whose YAML contains the string
  `self-hosted`.

When the criteria change this script will:
* **Spin up** a Docker container (based on the official
  `ghcr.io/actions/actions-runner` image) for newly‑active repos.
* **Shut down** and remove the container for repos that are no longer
  active.

The script relies exclusively on the `gh` CLI and Docker being
installed and authenticated.

Run it on a cadence (e.g. every 30 min) from a small server or CI job.
"""

import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List

import docker
from docker.models.containers import Container

###############################################################################
# Configuration
###############################################################################

active_threshold = timedelta(days=7)

docker_client = docker.from_env()

###############################################################################
# Small helpers
###############################################################################


def _run_gh(*args: str, **kwargs) -> str:
    """Run a gh command and return **stdout** (stripped)."""

    completed = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, **kwargs
    )
    return completed.stdout.strip()


def _iso_to_dt(value: str) -> datetime:
    """Convert ISO‑8601 strings (optionally ending in "Z") to aware datetimes."""

    value = value.rstrip("Z")
    if value[-1] in ["+", "-"] and ":" in value[-3:]:
        # Already has timezone information (e.g. "+00:00")
        return datetime.fromisoformat(value)
    # No explicit tz – assume UTC
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _is_recent(ts: datetime) -> bool:
    """`True` if *ts* is within the `active_threshold`."""

    return datetime.now(timezone.utc) - ts <= active_threshold

###############################################################################
# GitHub API helpers (all via the gh CLI)
###############################################################################


def get_gh_username() -> str:
    """Return the login of the authenticated GitHub user."""

    return _run_gh("api", "user", "--jq", ".login")


def _latest_commit_date(owner: str, repo: str) -> datetime | None:
    """Return the timestamp of the latest commit (or *None* on error)."""

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
    """Return the timestamp of the latest workflow run (or *None* if none)."""

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
    """Return *True* iff any workflow file references `self-hosted`."""

    # 1) List workflow files (ignore if folder missing)
    try:
        raw = _run_gh(
            "api", f"repos/{owner}/{repo}/contents/.github/workflows", "--jq", "."
        )
    except subprocess.CalledProcessError:
        return False  # No workflows folder

    try:
        files = json.loads(raw)
    except json.JSONDecodeError:
        return False

    for item in files:
        if item.get("type") != "file":
            continue
        path = item.get("path")
        if not path or not Path(path).suffix.lower() in {".yml", ".yaml"}:
            continue
        try:
            file_json = _run_gh("api", f"repos/{owner}/{repo}/contents/{path}")
            payload = json.loads(file_json)
            content_b64 = payload.get("content", "")
            content: str = base64.b64decode(content_b64).decode()
        except Exception:  # broad – any failure => ignore this file
            continue
        if "self-hosted" in content:
            return True
    return False

###############################################################################
# Determine which repos need a runner
###############################################################################


def get_active_repos(owner: str) -> List[str]:
    """Return repo **names** owned by *owner* that meet our criteria."""

    # Query all repos owned by user/org. Keep name + nameWithOwner for later.
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

    active_repos: list[str] = []
    for info in repo_infos:
        full = info["nameWithOwner"]  # "owner/repo"
        owner_, repo = full.split("/", 1)

        # 1) Quick pre‑filter: must have a self‑hosted workflow reference
        if not _has_self_hosted_workflow(owner_, repo):
            continue

        # 2) Activity check – latest commit OR latest workflow run within threshold
        commit_dt = _latest_commit_date(owner_, repo)
        run_dt = _latest_workflow_run_date(owner_, repo)

        if (commit_dt and _is_recent(commit_dt)) or (run_dt and _is_recent(run_dt)):
            active_repos.append(repo)

    return active_repos

###############################################################################
# Docker helpers (container lifecycle)
###############################################################################


def get_active_runners() -> List[str]:
    """Return repo names that currently have a running container."""

    containers = docker_client.containers.list()
    runners = []
    for container in containers:
        # Expected format: actions-<owner>-<repo>
        if container.name.startswith("actions-"):
            parts = container.name.split("-", 2)
            if len(parts) == 3:
                _, _, repo_part = parts
                runners.append(repo_part)
    return runners


###############################################################################
# Runner spin‑up / tear‑down
###############################################################################


def _get_reg_token(owner: str, repo: str) -> str:
    """Fetch a short‑lived registration token for a repo."""

    out = _run_gh(
        "api",
        "-X",
        "POST",
        f"/repos/{owner}/{repo}/actions/runners/registration-token",
    )
    return json.loads(out)["token"]


def spin_down_runner(owner: str, repo: str) -> None:
    name = f"actions-{owner}-{repo}"
    try:
        container = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return  # Already gone
    print(f"Stopping runner container for {owner}/{repo} …")
    container.stop()  # auto‑remove is set → container disappears after stop


def spin_up_runner(owner: str, repo: str) -> Container:
    token = _get_reg_token(owner, repo)
    name = f"actions-{owner}-{repo}"
    url = f"https://github.com/{owner}/{repo}"

    print(f"Starting runner container for {owner}/{repo} …")
    return docker_client.containers.run(
        image="ghcr.io/actions/actions-runner:latest",
        name=name,
        remove=True,
        detach=True,
        environment={"REG_TOKEN": token},
        command=(
            "sh -c '"
            "./config.sh --url {url} --token $REG_TOKEN --labels self-hosted && "
            "./run.sh'"
        ),
    )

###############################################################################
# Orchestration
###############################################################################


def update_runners(owner: str, active_repos: Iterable[str]) -> None:
    current_runners = set(get_active_runners())
    desired_runners = set(active_repos)

    to_stop = current_runners - desired_runners
    to_start = desired_runners - current_runners

    for repo in sorted(to_stop):
        spin_down_runner(owner, repo)
    for repo in sorted(to_start):
        spin_up_runner(owner, repo)


###############################################################################
# Entry point
###############################################################################


def main() -> None:
    owner = get_gh_username()
    print(f"Authenticated as: {owner}")

    print("Discovering active repositories …")
    active_repos = get_active_repos(owner)
    print(f"Active repos needing runners: {active_repos}")

    print("Reconciling runner set …")
    update_runners(owner, active_repos)
    print("✅ Done.")


if __name__ == "__main__":
    main()
