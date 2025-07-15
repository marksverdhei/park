import os
from datetime import timedelta
from typing import Iterable
from datetime import datetime
import docker
from docker.models.containers import Container
import json
import subprocess

BASE_IMAGE = os.getenv("ACTIONS_RUNNER_BASE_IMAGE", "ghcr.io/actions/actions-runner:latest")
DEPROVISION = False

docker_client = docker.from_env()
active_threshold = timedelta(weeks=1)
abandoned_threshold = timedelta(weeks=50)


def get_gh_username() -> str:
    """
    Retrieves the currently authenticated GitHub username via GitHub CLI.

    Prerequisites:
    - GitHub CLI (`gh`) must be installed and authenticated (e.g., `gh auth login` or GH_TOKEN env var).

    Returns:
        str: The GitHub username (login) of the authenticated user.

    Raises:
        RuntimeError: If the `gh api user --jq .login` call fails.
    """
    try:
        completed = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            check=True,
        )
        username = completed.stdout.strip()
        if not username:
            raise RuntimeError("Received empty username from GitHub CLI.")
        return username
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else "<no stderr>"
        raise RuntimeError(f"Failed to retrieve GitHub username: {stderr}")


def _last_action_valid(repo_full_name: str) -> bool:
    """Return the updatedAt time of the newest workflow run, or None."""
    created_filter = (datetime.now() - active_threshold).isoformat()
    res = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--created", ">" + created_filter,
            "-R", repo_full_name,
            "-L", "1",
            "--json", "updatedAt",
            "-q", ".[0].updatedAt",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        text=True,
    )
    print(repo_full_name, res.stdout)
    ts = res.stdout.strip()
    return ts is not None and bool(ts)

def get_active_repos() -> set[str]:
    """Return repos whose *latest* activity (push **or** workflow run)
    is within `active_threshold`."""
    result = subprocess.run(
        ["gh", "repo", "list", "--no-archived", "--json", "nameWithOwner,updatedAt", "--limit", "1000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )

    repos_json = json.loads(result.stdout)
    updated_time = {repo["nameWithOwner"]: datetime.fromisoformat(repo["updatedAt"].replace("Z", "")) for repo in repos_json}
    repo_names = {name for name, update in updated_time.items() if datetime.now() - update < abandoned_threshold}
    print(repo_names)
    action_active = {name for name in repo_names if _last_action_valid(name)}
    print(f"{action_active=}") 
    recent_activity = {
        name
        for name, ts in updated_time.items()
        if datetime.now() - ts < active_threshold
    }
    print(f"{recent_activity=}")    
    candidates = action_active | recent_activity
    print(candidates)

    # keep only repos that actually have Actions enabled / workflows defined
    candidates = filter_repos_with_actions(candidates)
    print(candidates)
    return candidates

def filter_repos_with_actions(repos: Iterable[str]) -> set[str]:
    """
    Filters repositories that use GitHub Actions at all.
    This is done by checking if the repository has a `.github/workflows` directory.
    """
    filtered_repos = set()
    # Check if the repo has a .github/workflows directory
    for repo_with_owner in repos:
        try:
            workflows_result = subprocess.run(
                ["gh", "api", f"/repos/{repo_with_owner}/contents/.github/workflows"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
            if workflows_result.stdout.strip():
                print(f"Repository {repo_with_owner} has workflows.")
                print(workflows_result.stdout)
                filtered_repos.add(repo_with_owner)
        except subprocess.CalledProcessError as e:
            print(f"Error checking repository {repo_with_owner}: {e.stderr.strip()}")
    

    return filtered_repos

def get_active_runners() -> list:
    """Gets repository runners"""
    # We only want the name of the repo, as we assume
    # The owner to be you. This will likely change in the future.
    return [
        "-".join(container.name.split("-")[2:])
        for container in docker_client.containers.list()
        if container.name.startswith("actions")
    ]


def get_reg_token(owner: str, repo: str) -> str:
    """
    Retrieves a GitHub Actions self-hosted runner registration token for the given repository.

    Prerequisites:
    - GitHub CLI (gh) must be installed and authenticated (e.g. `gh auth login` or GH_TOKEN env var).
    - The authenticated user must have admin access to the repository.

    Parameters:
        owner (str): GitHub username or organization owning the repo.
        repo (str): Repository name.

    Returns:
        str: A registration token that can be used to configure a self-hosted runner.

    Raises:
        RuntimeError: If the `gh api` call fails or the response is invalid.
    """
    try:
        # Call `gh api` to hit the REST endpoint:
        #   POST /repos/{owner}/{repo}/actions/runners/registration-token
        completed = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"/repos/{owner}/{repo}/actions/runners/registration-token",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else "<no stderr>"
        raise RuntimeError(f"Failed to request registration token: {stderr}")

    try:
        payload = json.loads(completed.stdout)
        token = payload.get("token")
        if not token:
            raise ValueError("No 'token' field in GitHub response.")
        return token
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"Unexpected response format: {e}")


def spin_down_runner(owner: str, repo: str) -> None:
    print(f"Shutting down container for {owner}/{repo}")
    runner_name = f"actions-{owner}-{repo}"
    container = docker_client.containers.get(runner_name)
    container.stop()
    print(f"Container '{repo}' was removed.")


def spin_up_runner(owner: str, repo: str) -> Container:
    print(f"Spinning up container for {owner}/{repo}")
    reg_token = get_reg_token(owner, repo)
    runner_name = f"actions-{owner}-{repo}"
    url = f"https://github.com/{owner}/{repo}"
    container = docker_client.containers.run(
        image=BASE_IMAGE,
        command=f"sh -c './config.sh --url {url} --token $REG_TOKEN && ./run.sh'",
        remove=True,
        detach=True,
        name=runner_name,
        environment={
            "REG_TOKEN": reg_token,
        },
        volumes={
            # mount the socket so `docker` commands inside talk to the host daemon
            '/var/run/docker.sock': {
                'bind': '/var/run/docker.sock',
                'mode': 'rw',
            },
            # (optional) mount the docker client binary, if your image doesn't already have it
            '/usr/bin/docker': {
                'bind': '/usr/bin/docker',
                'mode': 'ro',
            },
        }
    )
    print("Container started successfully")
    return container


def update_runners(
    owner: str, active_repos: Iterable[str], active_runners: list[str]
) -> None:
    "Starts and registers active runners, kills inactive runners."
    # Actions runners are prefixed with actions-<repo owner>/<repo name>
    repos_to_spin_down = set(active_runners) - set(active_repos)
    repos_to_spin_up = set(active_repos) - set(active_runners)
    print(repos_to_spin_down)

    for repo in repos_to_spin_down:
        owner, repo = repo.split("/", 1)
        spin_down_runner(owner, repo)

    for repo in repos_to_spin_up:
        owner, repo = repo.split("/", 1)
        spin_up_runner(owner, repo)


def main() -> None:
    print("Using base image:", BASE_IMAGE)
    username = get_gh_username()
    print(username)

    if DEPROVISION:
        print("Deprovisioning all containers")
        active_repos, active_runners = [], []
    else:
        print("Checking repos")
        active_repos = get_active_repos()
        print(active_repos)

    print("Checking runners")
    active_runners = get_active_runners()

    print(active_runners)
    print("Updating runners")

    update_runners(username, active_repos, active_runners)


if __name__ == "__main__":
    main()
