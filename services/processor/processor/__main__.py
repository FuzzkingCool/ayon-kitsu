"""Entrypoint for the Kitsu processor service. Logs to stderr so cloud/container logs are visible."""
import sys
import traceback

# Bootstrap: ensure container logs show we started (cloud workers often only capture stdout/stderr)
def _bootstrap():
    print("kitsu-processor: starting", file=sys.stderr, flush=True)
    # Fail fast if required env missing so logs are visible
    missing = [
        k for k in ("AYON_SERVER_URL", "AYON_API_KEY")
        if not __import__("os").environ.get(k)
    ]
    if missing:
        print(
            f"kitsu-processor: missing required env: {', '.join(missing)}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    _bootstrap()

    try:
        from nxtools import log_traceback, logging
        from .processor import (
            KitsuProcessor,
            KitsuServerError,
            KitsuSettingsError,
        )
    except Exception as e:
        print(f"kitsu-processor: import failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    def main():
        try:
            processor.start_processing()
        except Exception:
            log_traceback()
        sys.exit()

    try:
        processor = KitsuProcessor()
    except (KitsuServerError, KitsuSettingsError) as e:
        print(f"kitsu-processor: init error: {e}", file=sys.stderr, flush=True)
        logging.error(str(e))
        sys.exit(1)
    except Exception as e:
        print(f"kitsu-processor: init failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        log_traceback()
        sys.exit(1)

    main()
