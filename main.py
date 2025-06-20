from datetime import timedelta
from datetime import datetime
import docker
from docker.models.containers import Container
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


def get_active_repos() -> list:
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

    recent_activity = [k for k, v in repos.items() if datetime.now() - v < active_threshold]
    with_actions = filter_repos_with_actions(recent_activity)
    return with_actions

def filter_repos_with_actions(repos: list[str]) -> list[str]:
    """
    Filters repositories that use GitHub Actions at all.
    This is done by checking if the repository has a `.github/workflows` directory.
    """
    filtered_repos = []
    # Check if the repo has a .github/workflows directory
    owner = get_gh_username()
    for repo in repos:
        print(repo)
        try:
            workflows_result = subprocess.run(
                ["gh", "api", f"/repos/{owner}/{repo}/contents/.github/workflows"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
            if workflows_result.returncode == 0:
                filtered_repos.append(repo)
        except subprocess.CalledProcessError as e:
            print(f"Error checking repository {repo}: {e.stderr.strip()}")
    

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
        image="ghcr.io/actions/actions-runner:latest",
        command=f"sh -c './config.sh --url {url} --token $REG_TOKEN && ./run.sh'",
        remove=True,
        detach=True,
        name=runner_name,
        environment={
            "REG_TOKEN": reg_token,
        },
    )
    print("Container started successfully")
    return container


def update_runners(
    owner: str, active_repos: list[str], active_runners: list[str]
) -> None:
    "Starts and registers active runners, kills inactive runners."
    # Actions runners are prefixed with actions-<repo owner>/<repo name>
    repos_to_spin_down = set(active_runners) - set(active_repos)
    repos_to_spin_up = set(active_repos) - set(active_runners)
    print(repos_to_spin_down)

    for repo in repos_to_spin_down:
        spin_down_runner(owner, repo)

    for repo in repos_to_spin_up:
        spin_up_runner(owner, repo)


def main() -> None:
    username = get_gh_username()
    print(username)
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
