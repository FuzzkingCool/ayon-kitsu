import re
import subprocess
import sys

def tag_exists(tag: str) -> bool:
    """Check if a git tag exists."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return True
    except subprocess.CalledProcessError:
        return False


def get_version() -> str:
    """Read the project version from pyproject.toml using a regex."""
    with open("pyproject.toml", "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'version\s*=\s*"([^"]+)"', content)
    if not match:
        raise RuntimeError("Could not find version in pyproject.toml")
    return match.group(1)


def run_git_command(args):
    """Run a git command and exit on failure."""
    try:
        subprocess.run(["git", *args], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {' '.join(e.cmd)}", file=sys.stderr)
        sys.exit(1)


def main():
    version = get_version()
    tag = f"v{version}"
    if tag_exists(tag):
        run_git_command(["tag", "-d", tag])
        run_git_command(["push", "--delete", "origin", tag])
    run_git_command(["tag", tag])
    run_git_command(["push", "origin", tag])


if __name__ == "__main__":
    main()
