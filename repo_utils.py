import subprocess
import json

def repo_uses_self_hosted_runner(owner: str, repo: str) -> bool:
    """
    Checks if a repository uses GitHub Actions with at least one self-hosted runner in its workflow files.
    """
    try:
        # List workflow files in the repo
        completed = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{owner}/{repo}/actions/workflows",
                "--jq",
                ".workflows[].path"
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        workflow_paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not workflow_paths:
            return False
        # For each workflow file, check if it references 'runs-on: self-hosted'
        for path in workflow_paths:
            file_completed = subprocess.run(
                [
                    "gh",
                    "api",
                    f"/repos/{owner}/{repo}/contents/{path}",
                    "--jq",
                    ".content"
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            import base64
            import yaml
            content_b64 = file_completed.stdout.strip().strip('"')
            if not content_b64:
                continue
            try:
                content = base64.b64decode(content_b64).decode("utf-8")
                yml = yaml.safe_load(content)
                # Check for 'runs-on: self-hosted' in jobs
                jobs = yml.get("jobs", {})
                for job in jobs.values():
                    runs_on = job.get("runs-on")
                    if isinstance(runs_on, list):
                        if "self-hosted" in runs_on:
                            return True
                    elif runs_on == "self-hosted":
                        return True
            except Exception:
                continue
        return False
    except subprocess.CalledProcessError:
        return False
