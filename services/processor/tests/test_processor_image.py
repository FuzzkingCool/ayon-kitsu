#!/usr/bin/env python3
"""Build and run the Kitsu processor Docker image for testing.

This script:
1. Reads version from package.py
2. Loads environment variables from ../.env
3. Builds the Docker image
4. Runs the container with the environment variables
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
        "-t", image_name,
        "-f", str(dockerfile_path),
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
        "docker", "run", "--rm", "-it",
        "--hostname", "kitsu-dev-worker",
    ]

    # Add environment variables
    for key, value in env_vars.items():
        cmd.extend(["--env", f"{key}={value}"])

    # Add required AYON variables if not in .env
    required_vars = {
        "AYON_ADDON_NAME": "kitsu",
        "AYON_SERVICE_NAME": "processor",
    }

    for key, default_value in required_vars.items():
        if key not in env_vars:
            cmd.extend(["--env", f"{key}={default_value}"])

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
        print(f"\nAddon version: {version}")
    except Exception as e:
        print(f"ERROR: Failed to get version: {e}")
        sys.exit(1)

    # Load environment variables
    env_vars = load_env_file(env_file)
    if env_file.exists():
        print(f"\nLoaded {len(env_vars)} environment variables from {env_file}")
    else:
        print(f"\nWARNING: .env file not found at {env_file}")
        print("You may need to set AYON_API_KEY and AYON_SERVER_URL manually")

    # Check for required environment variables
    required = ["AYON_API_KEY", "AYON_SERVER_URL"]
    missing = [var for var in required if var not in env_vars and var not in os.environ]
    if missing:
        print(f"\nWARNING: Missing required environment variables: {', '.join(missing)}")
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
 
    # Build image
    image_name = f"ghcr.io/studioname/ayon-kitsu-processor:{version}"
    build_image(image_name, script_dir)

    # Run container
    run_container(image_name, env_vars)

if __name__ == "__main__":
    main()
