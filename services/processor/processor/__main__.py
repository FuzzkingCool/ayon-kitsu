"""Entrypoint for the Kitsu processor service. Logs to stderr so cloud/container logs are visible."""

import os
import sys
import traceback
from datetime import datetime


def _log(msg):
    """Log to stderr with timestamp."""
    timestamp = datetime.utcnow().isoformat()
    print(f"[{timestamp}] kitsu-processor: {msg}", file=sys.stderr, flush=True)


# Bootstrap: ensure container logs show we started (cloud workers often only capture stdout/stderr)
def _bootstrap():
    _log("=== CONTAINER STARTING ===")
    _log(f"Python version: {sys.version}")
    _log(f"Python executable: {sys.executable}")

    # Log ALL environment variables for debugging
    _log("Environment variables:")
    for key in sorted(os.environ.keys()):
        if (
            "PASSWORD" in key.upper()
            or "SECRET" in key.upper()
            or "KEY" in key.upper()
            or key.upper().endswith("_PWD")
        ):
            value = "***REDACTED***"
        else:
            value = os.environ[key]
        _log(f"  {key}={value}")

    # Check critical environment variables
    _log("Checking critical environment variables...")
    critical_vars = {
        "AYON_SERVER_URL": os.environ.get("AYON_SERVER_URL"),
        "AYON_API_KEY": "***SET***"
        if os.environ.get("AYON_API_KEY")
        else "NOT SET",
        "AYON_ADDON_NAME": os.environ.get("AYON_ADDON_NAME"),
        "AYON_ADDON_VERSION": os.environ.get("AYON_ADDON_VERSION"),
        "AYON_SERVICE_NAME": os.environ.get("AYON_SERVICE_NAME"),
    }

    for key, value in critical_vars.items():
        _log(f"  {key}: {value}")

    # Fail fast if required env missing so logs are visible
    missing = [
        k for k in ("AYON_SERVER_URL", "AYON_API_KEY") if not os.environ.get(k)
    ]
    if missing:
        _log(
            f"ERROR: Missing required environment variables: {', '.join(missing)}"
        )
        _log("Container will exit with code 1")
        sys.exit(1)

    _log("All required environment variables present")


if __name__ == "__main__":
    _bootstrap()

    _log("Importing dependencies...")
    try:
        from nxtools import log_traceback, logging

        _log("nxtools imported successfully")
        from .processor import (
            KitsuProcessor,
            KitsuServerError,
            KitsuSettingsError,
        )

        _log("processor module imported successfully")
    except Exception as e:
        _log(f"FATAL: Import failed: {e}")
        traceback.print_exc(file=sys.stderr)
        _log("Container will exit with code 1")
        sys.exit(1)

    def main():
        _log("Starting main processing loop...")
        try:
            processor.start_processing()
        except KeyboardInterrupt:
            _log("Received keyboard interrupt, shutting down...")
        except Exception as e:
            _log(f"FATAL: Processing loop crashed: {e}")
            log_traceback()
        finally:
            _log("Main processing loop ended")
        sys.exit()

    _log("Initializing KitsuProcessor...")
    try:
        processor = KitsuProcessor()
        _log("KitsuProcessor initialized successfully")
    except (KitsuServerError, KitsuSettingsError) as e:
        _log(f"FATAL: Initialization error: {e}")
        logging.error(str(e))
        _log("Container will exit with code 1")
        sys.exit(1)
    except Exception as e:
        _log(f"FATAL: Unexpected initialization error: {e}")
        traceback.print_exc(file=sys.stderr)
        log_traceback()
        _log("Container will exit with code 1")
        sys.exit(1)

    _log("=== PROCESSOR READY ===")
    main()
    _log("=== CONTAINER EXITING ===")
