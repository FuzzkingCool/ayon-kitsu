# ayon-kitsu-processor

A Dockerized processor service for MDHR's Kitsu integration pipeline.

## Overview
This Docker image provides a lightweight, portable processor service for handling Kitsu-related tasks in MDHR's workflow. It includes utilities for synchronization, logging, and utility functions to streamline asset processing.

## Docker Image

This image is published to GitHub Container Registry (ghcr.io) as a private package.

### Image Location
```
ghcr.io/studiomdhr/ayon-kitsu-processor:latest
ghcr.io/studiomdhr/ayon-kitsu-processor:<version> 
```

Replace `studiomdhr` with your studio's GitHub organization name.

### Authentication

To pull the private image, authenticate with GitHub Container Registry using a Personal Access Token (PAT) with `read:packages` scope:

```bash
echo $CR_PAT | docker login ghcr.io -u USERNAME --password-stdin
```

Or on Windows PowerShell:
```powershell
$env:CR_PAT | docker login ghcr.io -u USERNAME --password-stdin
```

Replace `USERNAME` with your GitHub username and set `CR_PAT` to your classic PAT token.

### Pulling the Image

```bash
# Pull latest
docker pull ghcr.io/studiomdhr/ayon-kitsu-processor:latest

# Pull specific version
docker pull ghcr.io/studiomdhr/ayon-kitsu-processor:v1.2.6
```

### Running the Container

```bash
docker run --rm \
  -e AYON_API_KEY=your_api_key \
  -e AYON_SERVER_URL=http://your-server:5000 \
  -e AYON_ADDON_NAME=kitsu \
  -e AYON_SERVICE_NAME=processor \
  -e AYON_ADDON_VERSION=1.2.6 \
  ghcr.io/studiomdhr/ayon-kitsu-processor:latest
```

**Required env:** `AYON_SERVER_URL`, `AYON_API_KEY`. `AYON_API_KEY` must be a **service** API key (created in AYON for the kitsu addon service), not a user token; otherwise you get 403 "Only services can enroll for jobs". For job enrollment also set `AYON_ADDON_NAME=kitsu`, `AYON_SERVICE_NAME=processor`, `AYON_ADDON_VERSION=<addon version>` in the container env.

**Cloud / ynput-cloud-worker:** The entrypoint writes bootstrap and errors to stderr so container logs should show `kitsu-processor: starting` and any failure message. If you see no logs, check that the worker captures stderr and that the container is actually starting (image pull, env, command).

**Alignment with original:** Single AYON connection attempt (no retry). Original used `ayon-python-api = "1.0.0rc3"`; current uses `^1.0.1`. If the server expects the older API, pin the dependency in `processor/pyproject.toml` accordingly.

## Development

### Local Build

Build the image locally:

```bash
make build
```

### Manual Push to ghcr.io

If you need to manually push (e.g., for testing):

1. Authenticate:
   ```bash
   echo $CR_PAT | docker login ghcr.io -u USERNAME --password-stdin
   ```

2. Build and push:
   ```bash
   make dist-ghcr
   ```

### Automated Builds

GitHub Actions automatically builds and pushes images on:
- Push to `main` or `develop` branches
- Tagged releases (e.g., `v1.2.6`)
- Manual workflow dispatch

The workflow uses `GITHUB_TOKEN` for authentication and automatically tags images with:
- Branch names (for branch pushes)
- Semantic version tags (for releases)
- `latest` (for default branch)
- SHA-based tags

## Makefile Targets

- `make build` - Build Docker image locally
- `make dist-ghcr` - Build and push to ghcr.io (requires authentication)
- `make clean` - Remove local images
- `make dev` - Build and run locally with environment variables
- `make run` - Run service without building (requires local Python environment)

## Environment Variables

See `example_env` for required environment variables:
- `AYON_SERVER_URL` - AYON server URL
- `AYON_API_KEY` - AYON API key
- `AYON_ADDON_NAME` - Addon name (default: kitsu)
- `AYON_ADDON_VERSION` - Addon version

 
