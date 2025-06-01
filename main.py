from datetime import timedelta
from datetime import datetime
import docker
import json
import subprocess

docker_client = docker.from_env()
active_threshold = timedelta(weeks=1)


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


def get_active_repos() -> dict:
    """Gets repository information with activity"""
    result = subprocess.run(
        ["gh", "repo", "list", "--json", "name,updatedAt", "--limit", "1000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )

    repos = json.loads(result.stdout)

    repos = {
        repo["name"]: datetime.fromisoformat(repo["updatedAt"].replace("Z", ""))
        for repo in repos
    }

    return [k for k, v in repos.items() if datetime.now() - v < active_threshold]


def get_active_runners() -> list:
    """Gets repository runners"""
    return [
        container.name
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


def spin_down_runner(repo_name: str) -> None:
    runner_name = f"actions-{repo_name.replace('/', '-')}"
    container = docker_client.containers.get(runner_name)
    container.stop()
    container.remove()
    print(f"Container '{repo_name}' was removed.")


def spin_up_runner(repo_name: str) -> None:
    print(repo_name)
    owner, repo = repo_name.split("/")
    reg_token = get_reg_token(owner, repo)
    runner_name = f"actions-{owner}-{repo}"
    url = f"https://github.com/{owner}/{repo}"
    container = docker_client.containers.run(
        image="ghcr.io/actions/actions-runner:latest",
        command=f"sh -c './config.sh --url {url} --token $REG_TOKEN && ./run.sh'",
        remove=True,
        detach=True,
        name=runner_name,
        environment={
            "REG_TOKEN": reg_token,
        },
    )


def update_runners(active_repos: list[str], active_runners: list[str]) -> None:
    "Starts and registers active runners, kills inactive runners."
    # Actions runners are prefixed with actions-<repo owner>/<repo name>
    repos_with_runners = [n.split("-")[1].join("/") for n in active_runners]
    repos_to_spin_down = set(repos_with_runners) - set(active_repos)
    repos_to_spin_up = set(active_repos) - set(repos_with_runners)

    for repo in repos_to_spin_down:
        spin_down_runner(repo)

    for repo in repos_to_spin_up:
        spin_up_runner(repo)


def main() -> None:
    username = get_gh_username()
    print(username)
    print("Checking repos")
    active_repos = get_active_repos()
    active_repos = [f"{username}/{repo}" for repo in active_repos]
    print("Checking runners")
    active_runners = get_active_runners()
    print("Updating runners")
    update_runners(active_repos, active_runners)


if __name__ == "__main__":
    main()
