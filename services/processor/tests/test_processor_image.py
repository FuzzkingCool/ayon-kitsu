#!/usr/bin/env python3
"""Build and run the Kitsu processor Docker image for testing.

This script:
1. Reads version from package.py
2. Loads environment variables from ../.env
3. Builds the Docker image
4. Runs the container with the environment variables

Poetry in ``pyproject.toml`` may use a PEP 440 *local* segment with ``+`` (e.g.
``1.2.6+prod.0.2.31``) because that validates for ``poetry install`` in the
image. OCI/Docker image tags must not contain ``+``, so repo ``package.py``
uses hyphens for the same logical release (e.g. ``1.2.6-prod.0.2.31``) and we
normalize defensively when tagging (see ``oci_image_tag``).
"""

import os
import subprocess
import sys
from pathlib import Path


def get_version():
    """Get the addon version from package.py."""
    script_dir = Path(__file__).parent
    package_py = script_dir.parent.parent.parent / "package.py"

    if not package_py.exists():
        raise FileNotFoundError(f"package.py not found at {package_py}")

    # Read and execute package.py to get version
    with open(package_py, "r") as f:
        content = {}
        exec(f.read(), content)
        return content.get("version", "latest")


def get_poetry_version(processor_dir: Path) -> str:
    """Version from ``services/processor/pyproject.toml`` (Poetry / PEP 440).

    Parsed without ``tomllib`` so this script runs on Python < 3.11.
    """
    pyproject = processor_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise FileNotFoundError(f"pyproject.toml not found at {pyproject}")
    in_poetry = False
    with pyproject.open("r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.split("#", 1)[0].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                header = stripped[1:-1].strip()
                in_poetry = header == "tool.poetry"
                continue
            if not in_poetry or not stripped.startswith("version"):
                continue
            if "=" not in stripped:
                continue
            _, rhs = stripped.split("=", 1)
            val = rhs.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                return val[1:-1]
    raise RuntimeError("pyproject.toml has no [tool.poetry] version = ... line")


def oci_image_tag(version: str) -> str:
    """Map a version string to a valid Docker/OCI image tag component."""
    return version.replace("+", "-")


def assert_addon_version_matches_poetry(processor_dir: Path, package_version: str) -> None:
    """Ensure addon and processor Poetry versions are one release (PEP 440 + vs OCI -)."""
    poetry_version = get_poetry_version(processor_dir)
    if oci_image_tag(poetry_version) != package_version:
        print(
            "ERROR: package.py `version` must match processor pyproject.toml "
            "[tool.poetry] version when `+` in Poetry is mapped to `-` for OCI.\n"
            f"  package.py: {package_version!r}\n"
            f"  pyproject.toml: {poetry_version!r} -> OCI {oci_image_tag(poetry_version)!r}",
            file=sys.stderr,
        )
        sys.exit(1)


def load_env_file(env_path):
    """Load environment variables from .env file."""
    env_vars = {}
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                # Parse KEY=VALUE
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    env_vars[key] = value
    return env_vars


def build_image(image_name, dockerfile_dir):
    """Build the Docker image."""
    print(f"Building Docker image: {image_name}")
    print(f"Using Dockerfile from: {dockerfile_dir}")

    # Build context should be the parent directory (services) since Dockerfile expects to be built from there
    build_context = dockerfile_dir.parent
    print(f"Build context: {build_context}")

    # Use the Dockerfile from the parent directory (processor), not from tests
    dockerfile_path = build_context / "Dockerfile"
    print(f"Using Dockerfile: {dockerfile_path}")

    cmd = [
        "docker",
        "build",
        "-t",
        image_name,
        "-f",
        str(dockerfile_path),
        str(build_context),  # Build context is the parent directory (services)
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(f"ERROR: Docker build failed with exit code {result.returncode}")
        sys.exit(1)

    print(f"Successfully built image: {image_name}")


def run_container(image_name, env_vars):
    """Run the Docker container with environment variables."""
    print(f"\nRunning container from image: {image_name}")

    cmd = [
        "docker",
        "run",
        "--rm",
        "-it",
        "--hostname",
        "kitsu-dev-worker",
    ]

    # Add environment variables
    for key, value in env_vars.items():
        cmd.extend(["--env", f"{key}={value}"])

    # Add image and command
    cmd.append(image_name)
    cmd.extend(["python", "-m", "processor"])

    print(f"Running: {' '.join(cmd[:10])}... (truncated)")
    print("\nStarting processor service...\n")

    # Run the container
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(f"\nContainer exited with code {result.returncode}")
        sys.exit(result.returncode)


def main():
    """Main function."""
    script_dir = Path(__file__).parent
    services_dir = script_dir.parent
    env_file = services_dir / ".env"

    print("=" * 60)
    print("Kitsu Processor Docker Image Builder & Runner")
    print("=" * 60)

    # Get version
    try:
        version = get_version()
        processor_dir = script_dir.parent
        assert_addon_version_matches_poetry(processor_dir, version)
        poetry_version = get_poetry_version(processor_dir)
        print(f"\nAddon version: {version}")
        print(f"Poetry version: {poetry_version} (aligned for OCI tag)")
    except Exception as e:
        print(f"ERROR: Failed to get version: {e}")
        sys.exit(1)

    # Load environment variables
    env_vars = load_env_file(env_file)
    if env_file.exists():
        print(
            f"\nLoaded {len(env_vars)} environment variables from {env_file}"
        )
    else:
        print(f"\nWARNING: .env file not found at {env_file}")
        print("You may need to set AYON_API_KEY and AYON_SERVER_URL manually")

    # Check for required environment variables
    required = ["AYON_API_KEY", "AYON_SERVER_URL"]
    missing = [
        var
        for var in required
        if var not in env_vars and var not in os.environ
    ]
    if missing:
        print(
            f"\nWARNING: Missing required environment variables: {', '.join(missing)}"
        )
        print("These should be set in .env file or your environment")
        response = input("\nContinue anyway? (y/N): ")
        if response.lower() != "y":
            print("Aborted.")
            sys.exit(1)

    # Merge with system environment (system env takes precedence)
    for key in required:
        if key in os.environ:
            env_vars[key] = os.environ[key]

    # Set version in env vars
    env_vars["AYON_ADDON_VERSION"] = version

    # Build image (tag must be OCI-safe; package.py uses hyphen form)
    image_tag = oci_image_tag(version)
    image_name = f"ghcr.io/fuzzkingcool/ayon-kitsu-processor:{image_tag}"
    build_image(image_name, script_dir)

    # Run container
    run_container(image_name, env_vars)


if __name__ == "__main__":
    main()
