from datetime import timedelta
from datetime import datetime
import docker
import json
import subprocess

docker_client = docker.from_env()
active_threshold = timedelta(weeks=1) 

def get_active_repos() -> dict:
    """Gets repository information with activity"""
    result = subprocess.run(
        ["gh", "repo", "list", "--json", "name,updatedAt", "--limit", "1000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True
    )

    repos = json.loads(result.stdout)

    repos = {
        repo["name"]: datetime.fromisoformat(repo["updatedAt"].replace("Z", "+00:00"))
        for repo in repos
    }

    return [k for k, v in repos.items() if datetime.now() - v < active_threshold]


def get_active_runners() -> list:
    """Gets repository runners"""
    return [container.name for container in docker_client.containers.list() if container.name.startswith('actions')]


def spin_down_runner(repo_name: str) -> None:
    runner_name = f"actions-{repo_name}"
    container = docker_client.containers.get(runner_name)
    container.stop()
    container.remove()
    print(f"Container '{repo_name}' was removed.")


def spin_up_runner(repo_name: str) -> None: 
    runner_name = f"actions-{repo_name}"
    docker_client.run(
        image="ghcr.io/actions/actions-runner:latest",
        command="echo hello world",
        remove=True,
        detach=True,
    )

def update_runners(active_repos: list[str], active_runners: list[str]) -> None:
    "Starts and registers active runners, kills inactive runners."
    # Actions runners are prefixed with actions-<repo owner>/<repo name>
    repos_with_runners = [n.split("-")[1] for n in active_runners]
    repos_to_spin_down = set(repos_with_runners) - set(active_repos)
    repos_to_spin_up = set(active_repos) - set(repos_with_runners)

    for repo in repos_to_spin_down:
        spin_down_runner(repo)

    for repo in repos_to_spin_up:
        spin_up_runner(repo)


def main() -> None:
    print("Checking repos")
    active_repos = get_active_repos()
    print("Checking runners")
    active_runners = get_active_runners()
    print(active_repos)
    print(active_runners)
    # print("Updating runners")
    # update_runners(active_repos, active_runners)


if __name__ == "__main__":
    main()
