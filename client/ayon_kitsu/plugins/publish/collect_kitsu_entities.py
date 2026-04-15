# -*- coding: utf-8 -*-
import gazu
import pyblish.api

from ayon_core.pipeline import KnownPublishError
from ayon_kitsu.pipeline import KitsuPublishContextPlugin
from ayon_kitsu.utils import resolve_canonical_kitsu_task


class CollectKitsuEntities(KitsuPublishContextPlugin):
    """Collect Kitsu entities according to the current context"""

    order = pyblish.api.CollectorOrder + 0.499
    label = "Kitsu entities"

    def process(self, context):
        project_entity = context.data["projectEntity"]
        project_id = project_entity["data"].get("kitsuProjectId")
        kitsu_project = None
        if project_id:
            kitsu_project = gazu.project.get_project(project_id)
        if not kitsu_project:
            project_name = context.data["projectName"]
            raise KnownPublishError(
                f"Project '{project_name}' not found in kitsu by id!"
            )

        context.data["kitsuProject"] = kitsu_project
        self.log.debug(f"Collect kitsu project: {kitsu_project}")

        filtered_instances = []
        for instance in context:
            folder_entity = instance.data.get("folderEntity")
            if folder_entity:
                filtered_instances.append(instance)

        if not filtered_instances:
            return

        kitsu_entities_by_id = {}
        for instance in filtered_instances:
            folder_entity = instance.data["folderEntity"]
            folder_path = folder_entity["path"]
            kitsu_id = folder_entity["data"].get("kitsuId")
            if not kitsu_id:
                raise KnownPublishError(
                    f"Kitsu id not available in AYON for '{folder_path}'"
                )

            kitsu_entity = kitsu_entities_by_id.get(kitsu_id)
            if not kitsu_entity:
                kitsu_entity = gazu.entity.get_entity(kitsu_id)
                if not kitsu_entity:
                    raise KnownPublishError(
                        f"{folder_path} was not found in kitsu!"
                    )
                kitsu_entities_by_id[kitsu_id] = kitsu_entity

            instance.data["kitsuEntity"] = kitsu_entity

            # Task entity
            task_entity = instance.data.get("taskEntity")
            if not task_entity:
                continue

            task_name = task_entity["name"]

            self.log.debug(f"Collect kitsu: {kitsu_entity}")

            kitsu_task = resolve_canonical_kitsu_task(
                task_entity,
                kitsu_entity,
                kitsu_entities_by_id=kitsu_entities_by_id,
                log=self.log,
            )

            if not kitsu_task:
                raise KnownPublishError(
                    f"Task {task_name} not found in kitsu "
                    f"(folder={folder_path}, task id={task_entity.get('id')})!"
                )

            ts = kitsu_task.get("task_status")
            if not isinstance(ts, dict) or not ts.get("short_name"):
                raise KnownPublishError(
                    "Kitsu task payload has no embedded task_status for publish "
                    f"(task id={kitsu_task.get('id')}, keys={sorted(kitsu_task.keys())}). "
                    "Check gazu/Kitsu API or task id / cache resolution."
                )

            kitsu_entities_by_id[kitsu_task["id"]] = kitsu_task

            instance.data["kitsuTask"] = kitsu_task
            self.log.debug(f"Collect kitsu task: {kitsu_task}")
