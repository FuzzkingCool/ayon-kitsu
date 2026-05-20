# -*- coding: utf-8 -*-
import os

import gazu
import pyblish.api

from ayon_core.pipeline import PublishXmlValidationError
from ayon_kitsu.pipeline import KitsuPublishContextPlugin
from ayon_kitsu.utils import (
    context_has_kitsu_family_instance,
    fetch_kitsu_entity_by_id,
    instance_has_kitsu_family,
    is_kitsu_entity_row,
    resolve_canonical_kitsu_task,
)


class CollectKitsuEntities(KitsuPublishContextPlugin):
    """Collect Kitsu entities according to the current context"""

    order = pyblish.api.CollectorOrder + 0.5001
    label = "Kitsu entities"

    def process(self, context):
        if not context_has_kitsu_family_instance(context):
            self.log.debug(
                "No instances with 'kitsu' family; skipping Kitsu entity collect."
            )
            return

        project_entity = context.data["projectEntity"]
        project_id = project_entity["data"].get("kitsuProjectId")
        kitsu_project = None
        if project_id:
            kitsu_project = gazu.project.get_project(project_id)
        if not kitsu_project:
            project_name = context.data["projectName"]
            raise PublishXmlValidationError(
                self,
                f"Project '{project_name}' not found in kitsu by id!",
                key="project_not_in_kitsu",
                formatting_data={"project_name": project_name},
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
            if not instance_has_kitsu_family(instance):
                self.log.debug(
                    "Skipping Kitsu entity/task collect for %s "
                    "(no kitsu family)",
                    instance.data.get("productName"),
                )
                continue

            folder_entity = instance.data["folderEntity"]
            folder_path = folder_entity["path"]
            folder_name = os.path.basename(folder_path.rstrip("/\\")) or folder_path

            kitsu_id = folder_entity["data"].get("kitsuId")
            if not kitsu_id:
                raise PublishXmlValidationError(
                    self,
                    f"Kitsu id not available in AYON for '{folder_path}'",
                    key="no_kitsu_id_on_folder",
                    formatting_data={
                        "folder_path": folder_path,
                        "folder_name": folder_name,
                    },
                )

            cached = kitsu_entities_by_id.get(kitsu_id)
            kitsu_entity = cached if is_kitsu_entity_row(cached) else None
            if not kitsu_entity:
                kitsu_entity = fetch_kitsu_entity_by_id(
                    kitsu_id, log=self.log
                )
                if not kitsu_entity:
                    self._raise_folder_not_on_kitsu(
                        folder_name,
                        folder_path,
                        kitsu_id,
                    )
                kitsu_entities_by_id[kitsu_id] = kitsu_entity

            instance.data["kitsuEntity"] = kitsu_entity

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
                # Stale folder kitsuId or deleted shot/asset: live entity read
                # must succeed before blaming the AYON task row.
                entity_row_id = (
                    kitsu_entity.get("id")
                    if isinstance(kitsu_entity, dict)
                    else None
                )
                folder_live = fetch_kitsu_entity_by_id(
                    kitsu_id, log=self.log
                )
                entity_live = (
                    fetch_kitsu_entity_by_id(entity_row_id, log=self.log)
                    if entity_row_id and entity_row_id != kitsu_id
                    else folder_live
                )
                if not folder_live or not entity_live:
                    self._raise_folder_not_on_kitsu(
                        folder_name,
                        folder_path,
                        kitsu_id,
                    )

                ayon_task_id = task_entity.get("id") or ""
                raise PublishXmlValidationError(
                    self,
                    (
                        f"Task {task_name} not found on Kitsu for folder "
                        f"{folder_name!r} ({folder_path}, "
                        f"ayon task id={ayon_task_id})!"
                    ),
                    key="task_not_on_kitsu_entity",
                    formatting_data={
                        "task_name": task_name,
                        "folder_path": folder_path,
                        "folder_name": folder_name,
                        "ayon_task_id": ayon_task_id,
                    },
                )

            ts = kitsu_task.get("task_status")
            if not isinstance(ts, dict) or not ts.get("short_name"):
                kitsu_task_id = kitsu_task.get("id") or ""
                raise PublishXmlValidationError(
                    self,
                    (
                        "Kitsu task payload has no embedded task_status "
                        f"for publish (task id={kitsu_task_id})."
                    ),
                    key="task_status_missing",
                    formatting_data={"kitsu_task_id": kitsu_task_id},
                )

            kitsu_entities_by_id[kitsu_task["id"]] = kitsu_task
            instance.data["kitsuTask"] = kitsu_task
            self.log.debug(f"Collect kitsu task: {kitsu_task}")

    def _raise_folder_not_on_kitsu(self, folder_name, folder_path, kitsu_id):
        raise PublishXmlValidationError(
            self,
            (
                f"Folder {folder_name!r} ({folder_path}) exists "
                f"in AYON but not on Kitsu (entity id={kitsu_id})!"
            ),
            key="folder_not_on_kitsu",
            formatting_data={
                "folder_path": folder_path,
                "folder_name": folder_name,
                "kitsu_id": kitsu_id,
            },
        )
