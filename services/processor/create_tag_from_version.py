import subprocess
import sys
from pathlib import Path


def tag_exists(tag: str) -> bool:
    """Check if a git tag exists."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def get_version() -> str:
    """Addon release version from repo-root package.py (matches zip / AYON / image tags)."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    package_py = repo_root / "package.py"
    if not package_py.is_file():
        raise RuntimeError(f"package.py not found at {package_py}")
    ns: dict = {}
    with package_py.open("r", encoding="utf-8") as f:
        exec(f.read(), ns)
    version = ns.get("version")
    if not version:
        raise RuntimeError("package.py defines no 'version'")
    return str(version)


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
