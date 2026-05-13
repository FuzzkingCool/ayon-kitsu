"""Local-only driver: one-shot Kitsu to AYON full sync without running the service loop.

Run from repo root with the **same env vars as the Kitsu processor service** (at
minimum ``AYON_SERVER_URL``, ``AYON_API_KEY``, ``AYON_ADDON_NAME``,
``AYON_ADDON_VERSION``), for example:

    python services/processor/tests/test_processor_image.py
    python services/processor/tests/test_processor_image.py --project MyAyonProject

PowerShell (session only), then run the script from any cwd:

    $env:AYON_SERVER_URL="https://your-studio.ayon.app"
    $env:AYON_API_KEY="your-service-api-key"
    $env:AYON_ADDON_NAME="kitsu"
    $env:AYON_ADDON_VERSION="1.2.3"

Optional: ``KITSU_CONCEPT_ENTITY_MODEL`` or ``KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL``
override ``concept_entity_model`` from JSON (e.g. ``per_kitsu_concept`` for legacy).
Omitted or blank ``concept_entity_model`` defaults to ``per_linked_entity`` in the
processor, matching the studio addon.

Before checking env vars, the script loads ``.env`` from (1) the **ayon-kitsu**
repo root, then (2) **``Path.cwd()``**. For each key, a **non-empty** value from
the cwd file wins over the repo file; an **empty** value in cwd does **not**
erase a non-empty value from repo (so a stray ``KITSU_PWD=`` in another repo's
``.env`` cannot wipe ``KITSU_PWD`` from ayon-kitsu's ``.env``).

For **AYON_*** and other keys, values already set in the process environment are
not overwritten. For **Kitsu** login (``KITSU_LOGIN``, ``KITSU_EMAIL``, ``KITSU_PWD``,
and ``KITSU_PASSWORD`` as an alias for the password), non-empty merged ``.env``
values **always** override the shell so a partial IDE/shell env cannot block the
repo ``.env``.

If ``nxtools`` is not installed in that interpreter, a tiny in-process stub is
registered so the script still runs (use the real ``nxtools`` in production).

Not intended for CI. Uses ``KitsuProcessor(start_listener_threads=False)`` so
Socket.IO / AYON event threads do not run during the one-shot fullsync (less
AYON contention). Uses ``os._exit`` after sync so the process exits despite
any remaining non-daemon state.

Does not inspect or assert AYON activity feed behavior (for example whether each
Version gets a "published a version" line, impersonation, or timestamps); see
``processor.content_sync`` module docstring for how preview vs comment sync
relates to activities.

One-shot **comment body repair** (re-run Kitsu comment sync after processor
logic changes; no structural fullsync)::

    python services/processor/tests/test_processor_image.py --repair-kitsu-comment-bodies
    python services/processor/tests/test_processor_image.py --repair-kitsu-comment-bodies -p MyAyonProject

``--dry-run`` does **not** apply here: comment repair always mutates AYON (it may
delete and recreate activities). Use ``--dry-run`` only with
``--repair-version-authors``.

After processor updates, this path re-hashes and re-posts Kitsu-sourced comment
activities. When ``ContentSyncSettings.review_version_link_enabled`` is true and
a review revision resolves to an AYON review version, the replicated body may gain
a trailing GFM link ``[Version N](...)`` to the web Products view (``web_ui_base_url``
or API host-derived origin). Idempotent re-runs skip duplicate links.

One-shot **review version author repair** (PATCH placeholder ``author`` on review
versions using Kitsu breadcrumbs from ``version.data``; optional dry-run)::

    python services/processor/tests/test_processor_image.py --repair-version-authors --dry-run -p MyAyonProject
    python services/processor/tests/test_processor_image.py --repair-version-authors -p MyAyonProject

    Write JSON for versions skipped with no AYON user mapping (Kitsu email / name
    not found on AYON)::

        python .../test_processor_image.py --repair-version-authors \\
            --repair-version-authors-skip-json skips.json
        python .../test_processor_image.py --repair-version-authors \\
            --repair-version-authors-skip-json -
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import traceback
import types
import unicodedata
from pathlib import Path


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (no python-dotenv dependency)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _merge_dotenv_repo_then_cwd(
    repo: dict[str, str], cwd: dict[str, str]
) -> dict[str, str]:
    """Merge two dotenv maps: cwd overrides repo only when cwd's value is non-empty."""
    keys = set(repo) | set(cwd)
    merged: dict[str, str] = {}
    for key in keys:
        cv = cwd.get(key, "")
        rv = repo.get(key, "")
        if str(cv).strip():
            merged[key] = cv
        elif str(rv).strip():
            merged[key] = rv
        else:
            merged[key] = cv or rv
    return merged


def _load_dotenv_for_local_sync() -> None:
    """Populate env from repo and cwd ``.env``.

    Non-Kitsu keys: only fill ``os.environ`` when the variable is unset or blank
    (do not override a value already exported in the shell).

    Kitsu login keys ``KITSU_LOGIN``, ``KITSU_EMAIL``, ``KITSU_PWD``: if the merged
    ``.env`` has a non-empty value, it **always** wins over the shell. Otherwise a
    half-set environment (e.g. IDE sets ``KITSU_EMAIL`` but not ``KITSU_PWD``, or a
    stale ``KITSU_PWD``) blocks ``.env`` and forces AYON Studio secrets, which then
    fails Kitsu auth for the same email.
    """
    here = Path(__file__).resolve()
    # test_processor_image.py -> tests -> processor -> services -> ayon-kitsu root
    repo_root = here.parents[3]
    repo_env = _parse_dotenv_file(repo_root / ".env")
    cwd_env = _parse_dotenv_file(Path.cwd() / ".env")
    merged = _merge_dotenv_repo_then_cwd(repo_env, cwd_env)
    for key, value in merged.items():
        if key in ("KITSU_LOGIN", "KITSU_EMAIL", "KITSU_PWD", "KITSU_PASSWORD"):
            continue
        if (os.environ.get(key) or "").strip():
            continue
        if value is None or not str(value).strip():
            continue
        os.environ[key] = str(value)

    # Processor reads KITSU_PWD only; accept KITSU_PASSWORD from .env as alias.
    pwd_merged = (merged.get("KITSU_PWD") or merged.get("KITSU_PASSWORD") or "").strip()
    if pwd_merged:
        os.environ["KITSU_PWD"] = pwd_merged

    # ``_kitsu_login_from_env`` uses ``KITSU_LOGIN`` before ``KITSU_EMAIL``. If the
    # shell has a stale ``KITSU_LOGIN`` but ``.env`` only defines ``KITSU_EMAIL``,
    # drop ``KITSU_LOGIN`` so the merged email is used (and vice versa).
    login_m = (merged.get("KITSU_LOGIN") or "").strip()
    email_m = (merged.get("KITSU_EMAIL") or "").strip()
    if login_m:
        os.environ["KITSU_LOGIN"] = login_m
        if not email_m:
            os.environ.pop("KITSU_EMAIL", None)
    if email_m:
        os.environ["KITSU_EMAIL"] = email_m
        if not login_m:
            os.environ.pop("KITSU_LOGIN", None)


def _ensure_nxtools_shim() -> None:
    """Register a minimal ``nxtools`` if the real package is not installed.

    The processor stack normally ships with ``nxtools``; local shells often use
    a Python that only has ``ayon_api`` / ``gazu``. This stub covers
    ``logging``, ``log_traceback``, and ``slugify`` as used by the processor.
    """
    if importlib.util.find_spec("nxtools") is not None:
        return
    import logging as std_logging

    def log_traceback(message: str = "") -> None:
        if message:
            print(message, file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)

    def slugify(
        value,
        *,
        separator: str = "-",
        **_kwargs: object,
    ) -> str:
        text = str(value if value is not None else "").strip().lower()
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^a-z0-9]+", separator, text)
        out = text.strip(separator)
        return out or "x"

    mod = types.ModuleType("nxtools")
    mod.logging = std_logging
    mod.log_traceback = log_traceback
    mod.slugify = slugify
    sys.modules["nxtools"] = mod


def _preflight_processor_env() -> None:
    """Fail fast with setup hints if required service env is missing.

    Avoids ``KitsuProcessor``'s long sleep when ``ayon_api.init_service`` cannot
    run (e.g. shell has no ``AYON_SERVER_URL``).
    """
    required = (
        "AYON_SERVER_URL",
        "AYON_API_KEY",
        "AYON_ADDON_NAME",
        "AYON_ADDON_VERSION",
    )
    missing = [name for name in required if not (os.environ.get(name) or "").strip()]
    if not missing:
        return
    lines = [
        "Missing required environment variable(s) for the processor / ayon_api service:",
        *(f"  - {name}" for name in missing),
        "",
        "Set them in this shell (PowerShell), then re-run:",
        '  $env:AYON_SERVER_URL="https://..."',
        '  $env:AYON_API_KEY="..."',
        '  $env:AYON_ADDON_NAME="kitsu"',
        '  $env:AYON_ADDON_VERSION="..."',
        "",
        "Bash:",
        '  export AYON_SERVER_URL="https://..."',
        '  export AYON_API_KEY="..."',
        '  export AYON_ADDON_NAME="kitsu"',
        '  export AYON_ADDON_VERSION="..."',
        "",
        "For Kitsu login when Studio secrets are not used, also set KITSU_LOGIN (or "
        "KITSU_EMAIL) and KITSU_PWD. See processor addon docs.",
        "",
        "If you keep secrets in a .env file, put AYON_* in ayon-kitsu/.env (repo root) "
        "or in .env in your current working directory; this script loads those before "
        "this check (exported AYON_* vars still win; KITSU_* login vars from .env win).",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a one-off Kitsu to AYON full sync for paired projects (local dev)."
    )
    parser.add_argument(
        "-p",
        "--project",
        metavar="NAME",
        help="AYON project name (pairing ayonProjectName). Omit to sync all paired projects.",
    )
    parser.add_argument(
        "--repair-version-authors",
        action="store_true",
        help=(
            "PATCH review version author away from kitsu-processor placeholder using "
            "Kitsu uploader resolution (paired projects). Use --dry-run to log only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Only with --repair-version-authors: log planned author PATCHes; do not mutate "
            "AYON. Not valid with --repair-kitsu-comment-bodies or default fullsync (those "
            "always write to AYON)."
        ),
    )
    parser.add_argument(
        "--repair-version-authors-skip-json",
        metavar="PATH",
        help=(
            "Only with --repair-version-authors: after the run, write a JSON array of "
            "review versions that had a Kitsu uploader but no matching AYON user login "
            "(PATH, or - for stdout)."
        ),
    )
    parser.add_argument(
        "--repair-kitsu-comment-bodies",
        action="store_true",
        help=(
            "Re-run Kitsu→AYON comment activity sync for all task comments (paired "
            "projects only). No fullsync. Use after comment body / preview appendix fixes."
        ),
    )
    args = parser.parse_args()
    if args.dry_run and not args.repair_version_authors:
        parser.error(
            "--dry-run is only supported with --repair-version-authors. "
            "--repair-kitsu-comment-bodies and fullsync always perform live AYON writes; "
            "large projects can run a long time with sparse logs (use INFO logging)."
        )
    skip_json = (args.repair_version_authors_skip_json or "").strip()
    if skip_json and not args.repair_version_authors:
        parser.error(
            "--repair-version-authors-skip-json requires --repair-version-authors."
        )
    project_filter = (args.project or "").strip() or None

    proc_pkg_root = Path(__file__).resolve().parents[1]
    if str(proc_pkg_root) not in sys.path:
        sys.path.insert(0, str(proc_pkg_root))

    _load_dotenv_for_local_sync()
    _ensure_nxtools_shim()
    _preflight_processor_env()
    from nxtools import log_traceback, logging
    # Processor uses logging.info; without handlers only WARNING+ appears, so a
    # slow settings / Kitsu / GET .../pairing phase looks like a hang after ayon_api
    # prints "Logged in as user ...".
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    from processor.fullsync import project_full_sync
    from processor.processor import (
        KitsuProcessor,
        KitsuServerError,
        KitsuSettingsError,
    )

    print(
        "Initializing KitsuProcessor (addon settings, Kitsu login, pairing list)...",
        flush=True,
    )
    try:
        processor = KitsuProcessor(start_listener_threads=False)
    except (KitsuServerError, KitsuSettingsError) as e:
        print(f"FATAL: {e}", file=sys.stderr, flush=True)
        raise SystemExit(1) from e
    except Exception:
        log_traceback("KitsuProcessor initialization failed")
        raise SystemExit(1)

    print(
        f"KitsuProcessor ready ({len(processor.pairing_list)} pairing row(s)).",
        flush=True,
    )
    pairs = processor.pairing_list
    if project_filter is not None:
        pairs = [p for p in pairs if p.get("ayonProjectName") == project_filter]

    if project_filter is not None and not pairs:
        names = sorted(
            n
            for n in (
                p.get("ayonProjectName") for p in processor.pairing_list
            )
            if n
        )
        print(
            f"No pairing row for AYON project {project_filter!r}. "
            f"Paired ayonProjectName values: {names}",
            file=sys.stderr,
            flush=True,
        )
        os._exit(1)

    if args.repair_version_authors:
        from processor.content_sync import repair_review_version_authors_for_paired_projects

        skip_rows: list[dict[str, object]] | None = [] if skip_json else None
        stats = repair_review_version_authors_for_paired_projects(
            processor,
            ayon_project_name=project_filter,
            dry_run=args.dry_run,
            skip_no_ayon_login_rows=skip_rows,
        )
        logging.info("Review version author repair finished: %s", stats)
        print(f"Review version author repair finished: {stats}", flush=True)
        if skip_rows is not None:
            payload = json.dumps(skip_rows, indent=2)
            if skip_json == "-":
                print(payload, flush=True)
            else:
                Path(skip_json).write_text(payload, encoding="utf-8")
                print(
                    f"Wrote {len(skip_rows)} no-AYON-login skip row(s) to {skip_json!r}.",
                    flush=True,
                )
        os._exit(0)

    if args.repair_kitsu_comment_bodies:
        from processor.content_sync import repair_kitsu_comment_activities_for_paired_projects

        print(
            "Starting Kitsu comment body repair (live AYON mutations; progress on stderr)...",
            flush=True,
        )
        stats = repair_kitsu_comment_activities_for_paired_projects(
            processor,
            ayon_project_name=project_filter,
        )
        logging.info("Kitsu comment repair finished: %s", stats)
        print(f"Kitsu comment repair finished: {stats}", flush=True)
        os._exit(0)

    for pair in pairs:
        project_id = pair.get("kitsuProjectId")
        project_name = pair.get("ayonProjectName")
        if not project_id or not project_name:
            continue
        logging.info(
            f"Syncing project: {project_name} (Kitsu ID: {project_id})"
        )
        try:
            project_full_sync(processor, project_id, project_name)
        except Exception:
            log_traceback(f"Full sync failed for {project_name}")
            os._exit(1)

    logging.info("One-off full sync finished for all selected projects.")
    print("One-off full sync finished.", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
