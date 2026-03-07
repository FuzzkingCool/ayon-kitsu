# -*- coding: utf-8 -*-
"""Merge multiple Kitsu-only review instances for the same task into one.

When the user creates several review instances for the same task (e.g. one per
file), this collector merges them into a single instance with all
representations, so we get one Kitsu note and one revision with multiple items.
"""

import os

import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishContextPlugin

# Prefer video as main review file when merging
VIDEO_EXTENSIONS = {".mov", ".mp4", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg"}


class CollectMergeKitsuOnlyReview(KitsuPublishContextPlugin):
    """Merge same-task Kitsu-only review instances into one."""

    label = "Merge Kitsu-only review instances"
    order = pyblish.api.CollectorOrder + 0.505
    families = ["kitsu"]

    def process(self, context):
        # Group Kitsu-only review instances by task id
        by_task = {}
        for instance in context:
            if not instance.data.get("kitsuOnlyReview", False):
                continue
            kitsu_task = instance.data.get("kitsuTask")
            if not kitsu_task:
                continue
            task_id = kitsu_task["id"]
            by_task.setdefault(task_id, []).append(instance)

        for task_id, instances in by_task.items():
            if len(instances) <= 1:
                continue

            self.log.info(
                f"Merging {len(instances)} Kitsu-only review instance(s) for task {task_id} into one"
            )

            primary = instances[0]
            primary_reps = list(primary.data.get("representations", []))
            seen_paths = set()
            for rep in primary_reps:
                staging = rep.get("stagingDir") or ""
                for f in (rep.get("files") or []) if isinstance(rep.get("files"), list) else [rep.get("files") or ""]:
                    if f:
                        seen_paths.add((staging, f))

            for instance in instances[1:]:
                reps = instance.data.get("representations", [])
                for rep in reps:
                    staging_dir = rep.get("stagingDir") or ""
                    files = rep.get("files")
                    if isinstance(files, str):
                        files = [files]
                    for f in files or []:
                        if not f or (staging_dir, f) in seen_paths:
                            continue
                        seen_paths.add((staging_dir, f))
                        new_rep = dict(rep)
                        new_rep["files"] = f if isinstance(rep.get("files"), str) else [f]
                        new_rep["stagingDir"] = staging_dir
                        tags = list(new_rep.get("tags") or [])
                        if "kitsureview" not in tags:
                            tags.append("kitsureview")
                        new_rep["tags"] = tags
                        primary_reps.append(new_rep)

                instance.data["kitsuMergedInto"] = True
                instance.data["publish"] = False

            primary.data["representations"] = primary_reps

            # Merge reviewFilePaths so integrate uploads all files
            all_paths = set()
            for inst in instances:
                for p in inst.data.get("reviewFilePaths") or []:
                    if p and os.path.exists(p):
                        all_paths.add(os.path.normpath(p))
            if all_paths:
                primary.data["reviewFilePaths"] = sorted(all_paths)

            # Ensure reviewFile is set: prefer video, else first file
            if not primary.data.get("reviewFile") or not os.path.exists(primary.data["reviewFile"]):
                first_video = None
                first_any = None
                for rep in primary_reps:
                    staging_dir = rep.get("stagingDir") or ""
                    files = rep.get("files")
                    if isinstance(files, str):
                        files = [files]
                    for f in files or []:
                        path = os.path.join(staging_dir, f)
                        if os.path.exists(path):
                            if first_any is None:
                                first_any = path
                            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                                first_video = path
                                break
                primary.data["reviewFile"] = first_video or first_any or primary.data.get("reviewFile", "")
            self.log.debug(
                f"Merged to one instance with {len(primary_reps)} representation(s), reviewFilePaths={len(primary.data.get('reviewFilePaths') or [])}"
            )
