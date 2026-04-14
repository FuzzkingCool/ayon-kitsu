# -*- coding: utf-8 -*-
import gazu
import pyblish.api

from ayon_core.pipeline import KnownPublishError
from ayon_kitsu.pipeline import KitsuPublishContextPlugin


def _kitsu_task_type_lookup_name(task_entity):
    """Name to pass to gazu.task.get_task_type_by_name when kitsuId is absent.

    Checklist-style AYON tasks may use a slug ``name`` that is not a Kitsu task type.
    When ``name`` and ``taskType.name`` differ (case-insensitive), use the type name
    so the canonical Kitsu task row for that pipeline type is resolved.
    """
    task_name = (task_entity.get("name") or "").strip()
    tt = task_entity.get("taskType")
    if isinstance(tt, dict):
        task_type_name = (tt.get("name") or "").strip()
    elif isinstance(tt, str):
        task_type_name = tt.strip()
    else:
        task_type_name = ""
    if task_type_name and task_name.lower() != task_type_name.lower():
        return task_type_name
    return task_name


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
            kitsu_task_id = task_entity["data"].get("kitsuId")

            self.log.debug(f"Collect kitsu: {kitsu_entity}")

            if kitsu_task_id:
                kitsu_task = kitsu_entities_by_id.get(
                    kitsu_task_id
                ) or gazu.task.get_task(kitsu_task_id)
            else:
                lookup_name = _kitsu_task_type_lookup_name(task_entity)
                if not lookup_name:
                    raise KnownPublishError(
                        "Cannot resolve Kitsu task type: AYON task has empty name or "
                        f"taskType (folder={folder_path}, task id={task_entity.get('id')})."
                    )
                if lookup_name != task_name:
                    self.log.info(
                        "Kitsu task lookup redirect: AYON task id=%s name=%r != taskType; "
                        "using type name %r for get_task_type_by_name on Kitsu entity %s",
                        task_entity.get("id"),
                        task_name,
                        lookup_name,
                        kitsu_entity.get("id"),
                    )
                kitsu_task_type = gazu.task.get_task_type_by_name(lookup_name)
                if not kitsu_task_type:
                    raise KnownPublishError(
                        f"Task type {lookup_name!r} not found in Kitsu "
                        f"(AYON task name={task_name!r}, taskType={task_entity.get('taskType')!r})."
                    )

                kitsu_task = gazu.task.get_task_by_name(
                    kitsu_entity, kitsu_task_type
                )
                if kitsu_task and lookup_name != task_name:
                    self.log.info(
                        "Kitsu task resolved for redirect: kitsu_task_id=%s kitsu name=%r",
                        kitsu_task.get("id"),
                        kitsu_task.get("name"),
                    )

            if not kitsu_task:
                raise KnownPublishError(
                    f"Task {task_name} not found in kitsu!"
                )

            kitsu_entities_by_id[kitsu_task["id"]] = kitsu_task

            instance.data["kitsuTask"] = kitsu_task
            self.log.debug(f"Collect kitsu task: {kitsu_task}")
