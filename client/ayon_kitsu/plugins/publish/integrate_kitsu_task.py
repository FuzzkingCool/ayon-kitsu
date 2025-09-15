#! -*- coding: utf-8 -*-
import traceback

import gazu
import pyblish.api
from ayon_kitsu.pipeline import KitsuPublishContextPlugin


class IntegrateKitsuTask(KitsuPublishContextPlugin):
    """Update Kitsu Task Status on publish"""

    order = pyblish.api.IntegratorOrder
    label = "Kitsu Task Status"
    families = ["kitsu"]

    # task status settings
    set_status_task = False
    task_status_shortname = "wfa"
    status_change_conditions = {
        "status_conditions": [],
        "family_requirements": [],
    }

    def process(self, context):
        # Backwards compatibility for wrong key
        if "product_type_requirements" in self.status_change_conditions:
            family_requirements = self.status_change_conditions[
                "product_type_requirements"
            ]
        else:
            family_requirements = self.status_change_conditions[
                "family_requirements"
            ]

        if not self.set_status_task:
            return

        for instance in context:
            # Check if instance is a review by checking its family
            # Allow a match to primary family or any of families
            families = set(
                [instance.data["family"]] + instance.data.get("families", [])
            )
            if "review" not in families or "kitsu" not in families:
                continue

            kitsu_task = instance.data.get("kitsuTask")
            if not kitsu_task:
                continue

            # Determine current task status shortname
            current_shortname = kitsu_task["task_status"]["short_name"].upper()

            # Check if any status condition is not met
            allow_status_change = True
            for status_cond in self.status_change_conditions["status_conditions"]:
                condition = status_cond["condition"] == "equal"
                match = status_cond["short_name"].upper() == current_shortname
                if (match and not condition) or (condition and not match):
                    allow_status_change = False
                    break

            if allow_status_change:
                # Get families of published instances (normalized, include sub-families)
                published_families = set()
                for inst in context:
                    if not inst.data.get("publish"):
                        continue
                    main_family = (inst.data.get("family") or "").lower()
                    if main_family:
                        published_families.add(main_family)
                    for sub_family in inst.data.get("families", []) or []:
                        sub_family_l = (sub_family or "").lower()
                        if sub_family_l:
                            published_families.add(sub_family_l)

                # Check family requirements – approve if ANY requirement matches
                # any published family. If no requirements are defined, leave as-is.
                if family_requirements:
                    allow_status_change = False
                    for family_requirement in family_requirements:
                        condition_equal = (
                            family_requirement["condition"] == "equal"
                        )

                        # Prefer 'product_type' (current) or fallback to 'family' (legacy)
                        requirement_value = (
                            family_requirement.get("product_type")
                            or family_requirement.get("family")
                            or ""
                        ).lower()

                        if condition_equal:
                            if requirement_value in published_families:
                                allow_status_change = True
                                break
                        else:
                            if requirement_value not in published_families:
                                allow_status_change = True
                                break

            if not allow_status_change:
                continue

            desired_shortname = self.task_status_shortname
            self.log.info(
                f"Attempting to set Kitsu task status to '{desired_shortname}'"
            )

            try:
                status_entity = gazu.task.get_task_status_by_short_name(
                    desired_shortname
                )
                if not status_entity:
                    self.log.info(
                        f"Cannot find task status shortname '{desired_shortname}'."
                    )
                    continue

                # Update task status in Kitsu
                # Create a copy of the task dict and update the task_status field
                updated_task = kitsu_task.copy()
                updated_task["task_status_id"] = status_entity["id"]

                gazu.task.update_task(updated_task)
                self.log.info(
                    f"Task '{kitsu_task['id']}' status set to '{desired_shortname}'."
                )
            except Exception as exc:
                self.log.error(
                    f"Error updating Kitsu task status to '{desired_shortname}': {exc}"
                )
                self.log.error(traceback.format_exc())


