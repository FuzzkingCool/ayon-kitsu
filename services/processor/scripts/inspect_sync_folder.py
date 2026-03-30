"""Resolve AYON folder_id + task name to human-readable path (uses .env for API)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running without installing the processor package
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROCESSOR_ROOT = _SCRIPT_DIR.parent
if str(_PROCESSOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROCESSOR_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="MyGame")
    parser.add_argument(
        "--folder-id",
        default="6ad385e6-68d2-11f0-80b5-aa2cb6e9d46b",
        help="AYON folder UUID from sync error",
    )
    parser.add_argument("--task-name", default="tiedown")
    parser.add_argument(
        "--kitsu-task-id",
        default="",
        help="Kitsu task UUID from fullsync error; compares to AYON task data.kitsuId",
    )
    args = parser.parse_args()

    from dotenv import find_dotenv, load_dotenv

    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path)
        print(f"Loaded .env from {env_path}", file=sys.stderr)
    else:
        load_dotenv()
        print("No .env found via find_dotenv; using process env only", file=sys.stderr)

    if not os.environ.get("AYON_SERVER_URL") or not os.environ.get("AYON_API_KEY"):
        print(
            "Set AYON_SERVER_URL and AYON_API_KEY (e.g. in .env next to this repo).",
            file=sys.stderr,
        )
        return 1

    import ayon_api

    ayon_api.init_service()

    folder = ayon_api.get_folder_by_id(args.project, args.folder_id)
    if not folder:
        print(f"No folder {args.folder_id!r} in project {args.project!r}", file=sys.stderr)
        return 2

    keys = (
        "id",
        "name",
        "label",
        "path",
        "folderType",
        "parentId",
    )
    summary = {k: folder.get(k) for k in keys if k in folder}
    print(json.dumps({"folder": summary}, indent=2, default=str))

    task = ayon_api.get_task_by_name(
        args.project,
        args.folder_id,
        args.task_name,
        fields={"id", "name", "label", "taskType", "status", "assignees", "data"},
    )
    if task:
        tkeys = ("id", "name", "label", "taskType", "status", "assignees", "data")
        tsum = {k: task.get(k) for k in tkeys if k in task}
        out = {"existing_ayon_task": tsum}
        kitsu_on_ayon = (task.get("data") or {}).get("kitsuId")
        out["ayon_task_data_kitsuId"] = kitsu_on_ayon
        if args.kitsu_task_id:
            k = args.kitsu_task_id.strip()
            out["kitsu_task_id_from_error"] = k
            same = (kitsu_on_ayon or "").replace("-", "").lower() == k.replace(
                "-", ""
            ).lower()
            out["ids_match_for_sync_lookup"] = bool(kitsu_on_ayon) and same
            if not same:
                out["sync_diagnosis"] = (
                    "Push looks up AYON tasks by data.kitsuId only. "
                    "If kitsuId is missing or differs from this Kitsu task id, "
                    "sync tries to CREATE a new task → same name → unique error. "
                    "You still see a single 'tiedown' in both UIs."
                )
        print(json.dumps(out, indent=2, default=str))
    else:
        print(json.dumps({"existing_ayon_task": None}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
