# -*- coding: utf-8 -*-
import traceback

import pyblish.api
from ayon_core.pipeline import get_current_project_name
from ayon_harmony.logger import log as log_harmony
from ayon_kitsu.pipeline import (
    KitsuPublishContextPlugin,
)


class CollectKitsuLatestReviewVersion(KitsuPublishContextPlugin):
    """Collect latest Kitsu review version for the task context.

    Query Kitsu for existing previews on any task with Kitsu context and store
    the latest version number so that ALL instances in the task can access it
    for version coordination (workfiles, renders, reviews, etc.).

    This plugin runs on ALL instances regardless of families to ensure universal
    Kitsu version coordination.
    """

    label = "Kitsu Latest Review Revision"
    order = pyblish.api.CollectorOrder + 0.479
    # Remove families restriction - process ALL instances
    log = log_harmony
    log.debug("CollectKitsuLatestReviewVersion plugin loaded")

    def process(self, context):
        self.log.info(
            "[KitsuLatestReview] Collecting Kitsu versions for ALL tasks in context"
        )

        # Group ALL instances by task context (folderPath + task name)
        task_groups = {}

        for instance in context:
            folder_path = instance.data.get("folderPath")
            task_name = instance.data.get("task")

            if not folder_path or not task_name:
                continue

            task_key = f"{folder_path}::{task_name}"
            if task_key not in task_groups:
                task_groups[task_key] = []
            task_groups[task_key].append(instance)

        if not task_groups:
            self.log.debug("[KitsuLatestReview] No valid task contexts found")
            return

        self.log.info(
            f"[KitsuLatestReview] Found {len(task_groups)} task contexts to check"
        )

        # For each task context, get Kitsu version and apply to all instances
        for task_key, instances in task_groups.items():
            folder_path, task_name = task_key.split("::", 1)
            self.log.info(
                f"[KitsuLatestReview] Processing task context: {task_key}"
            )

            # Get Kitsu latest version for this task context
            latest_version = self._get_kitsu_latest_version_for_task(
                folder_path, task_name
            )

            # Apply to ALL instances in this task context
            for instance in instances:
                instance.data["kitsuLatestVersion"] = latest_version
                self.log.debug(
                    f"[KitsuLatestReview] Set kitsuLatestVersion={latest_version} "
                    f"for {instance.data.get('productName', 'unknown')}"
                )

    def _get_kitsu_latest_version_for_task(self, folder_path, task_name):
        """Get the latest Kitsu version for a task using AYON folder path and task name.

        Args:
            folder_path (str): AYON folder path
            task_name (str): AYON task name

        Returns:
            int: Latest version number from Kitsu (0 if no Kitsu task found)
        """
        try:
            import ayon_api
            import gazu

            # Get AYON project from context
            project_name = get_current_project_name()
            if not project_name:
                self.log.debug(
                    "[KitsuLatestReview] No current project name available"
                )
                return 0

            # Get folder entity from AYON
            folder_entity = ayon_api.get_folder_by_path(
                project_name, folder_path
            )
            if not folder_entity:
                self.log.debug(
                    f"[KitsuLatestReview] Folder not found in AYON: {folder_path}"
                )
                return 0

            # Get task entity from AYON
            task_entity = ayon_api.get_task_by_name(
                project_name, folder_entity["id"], task_name
            )
            if not task_entity:
                self.log.debug(
                    f"[KitsuLatestReview] Task not found in AYON: {task_name}"
                )
                return 0

            # Get Kitsu IDs from AYON entities
            kitsu_folder_id = folder_entity.get("data", {}).get("kitsuId")
            kitsu_task_id = task_entity.get("data", {}).get("kitsuId")

            if not kitsu_folder_id:
                self.log.debug(
                    f"[KitsuLatestReview] No Kitsu folder ID for: {folder_path}"
                )
                return 0

            # Find Kitsu task - use explicit ID if available, otherwise query by name
            if kitsu_task_id:
                kitsu_task = gazu.task.get_task(kitsu_task_id)
            else:
                # Fallback: find task by folder and task type name
                kitsu_entity = gazu.entity.get_entity(kitsu_folder_id)
                if not kitsu_entity:
                    self.log.debug(
                        f"[KitsuLatestReview] Kitsu entity not found: {kitsu_folder_id}"
                    )
                    return 0

                kitsu_task_type = gazu.task.get_task_type_by_name(task_name)
                if not kitsu_task_type:
                    self.log.debug(
                        f"[KitsuLatestReview] Kitsu task type not found: {task_name}"
                    )
                    return 0

                kitsu_task = gazu.task.get_task_by_name(
                    kitsu_entity, kitsu_task_type
                )

            if not kitsu_task:
                self.log.debug(
                    f"[KitsuLatestReview] Kitsu task not found: {folder_path}/{task_name}"
                )
                return 0

            # Query Kitsu for previews and find highest revision
            return self._query_kitsu_previews(kitsu_task)

        except Exception as exc:
            self.log.warning(
                f"[KitsuLatestReview] Failed to get Kitsu version for {folder_path}/{task_name}: {exc}"
            )
            self.log.debug(traceback.format_exc())
            return 0

    def _query_kitsu_previews(self, kitsu_task):
        """Query Kitsu previews for a task and return the highest revision.

        Args:
            kitsu_task (dict): Kitsu task data

        Returns:
            int: Highest revision number found
        """
        try:
            import gazu

            task_id = kitsu_task["id"]
            self.log.info(
                f"[KitsuLatestReview] Querying previews for task_id={task_id}"
            )

            # Use the correct Gazu API - get all preview files for the task
            try:
                # Use the correct function from gazu.files module
                import gazu.files

                previews = gazu.files.get_all_preview_files_for_task(
                    kitsu_task
                )
                self.log.info(
                    "[KitsuLatestReview] Successfully got previews via Gazu files API"
                )
            except Exception as e:
                self.log.info(
                    f"[KitsuLatestReview] Gazu files API failed: {e}, trying REST endpoint"
                )
                # Fallback to direct REST API call
                previews = gazu.client.get(f"data/tasks/{task_id}/previews")
                self.log.info(
                    "[KitsuLatestReview] Got previews via REST endpoint"
                )

            # Normalize response
            if hasattr(previews, "json"):
                try:
                    previews = previews.json()
                except Exception:
                    pass

            count = (
                len(previews)
                if isinstance(previews, (list, tuple))
                else "unknown"
            )
            self.log.info(f"[KitsuLatestReview] Retrieved {count} previews")

            # Find highest revision
            latest = 0
            revisions = []
            for preview in previews or []:
                value = preview.get("revision")
                try:
                    rev = int(value) if value is not None else 0
                    if rev > latest:
                        latest = rev
                    revisions.append(rev)
                except Exception:
                    continue

            self.log.info(
                f"[KitsuLatestReview] Found Kitsu latest version {latest}"
            )
            if revisions:
                self.log.debug(
                    f"[KitsuLatestReview] All revisions: {sorted(revisions)}"
                )

            return latest

        except Exception as exc:
            self.log.warning(
                f"[KitsuLatestReview] Failed to query Kitsu previews: {exc}"
            )
            return 0
