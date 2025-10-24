# -*- coding: utf-8 -*-
import re
import traceback

import gazu
import pyblish.api
from ayon_kitsu.pipeline import KitsuPublishContextPlugin


class IntegrateKitsuNote(KitsuPublishContextPlugin):
    """Integrate Kitsu Note"""

    order = pyblish.api.IntegratorOrder
    label = "Kitsu Note and Status"
    families = ["kitsu"]

    # status settings
    set_status_note = False
    note_status_shortname = "wfa"
    status_change_conditions = {
        "status_conditions": [],
        "family_requirements": [],
    }

    # comment settings
    custom_comment_template = {
        "enabled": False,
        "comment_template": "{comment}",
    }

    def format_publish_comment(self, instance):
        """Format the instance's publish comment

        Formats `instance.data` against the custom template.
        """

        def replace_missing_key(match):
            """If key is not found in kwargs, set None instead"""
            key = match.group(1)
            if key not in instance.data:
                self.log.warning(
                    "Key '{}' was not found in instance.data "
                    "and will be rendered as an empty string "
                    "in the comment".format(key)
                )
                return ""
            else:
                return str(instance.data[key])

        template = self.custom_comment_template["comment_template"]
        pattern = r"\{([^}]*)\}"
        return re.sub(pattern, replace_missing_key, template)

    def process(self, context):
        # Backwards compatibility for wront key
        if "product_type_requirements" in self.status_change_conditions:
            family_requirements = self.status_change_conditions[
                "product_type_requirements"
            ]
        else:
            family_requirements = self.status_change_conditions[
                "family_requirements"
            ]

        # Detect grouped vs single review per Kitsu task
        # Group instances that are review+kitsu by task id
        by_task = {}
        for instance in context:
            families = set([instance.data["family"]] + instance.data.get("families", []))
            if "review" not in families or "kitsu" not in families:
                continue
            kitsu_task = instance.data.get("kitsuTask")
            if not kitsu_task:
                continue
            task_id = kitsu_task["id"]
            by_task.setdefault(task_id, []).append(instance)

        for task_id, instances in by_task.items():
            kitsu_task = instances[0].data.get("kitsuTask")
            if not kitsu_task:
                continue
            is_grouped = len(instances) > 1

            # Get note status, by default uses the task status for the note
            # if it is not specified in the configuration
            shortname = kitsu_task["task_status"]["short_name"].upper()
            note_status = kitsu_task["task_status_id"]

            # Check if any status condition is not met
            allow_status_change = True
            for status_cond in self.status_change_conditions[
                "status_conditions"
            ]:
                condition = status_cond["condition"] == "equal"
                match = status_cond["short_name"].upper() == shortname
                if match and not condition or condition and not match:
                    allow_status_change = False
                    break

            if allow_status_change:
                # Get families of published instances (normalized, include sub-families)
                families = set()
                for inst in context:
                    if not inst.data.get("publish"):
                        continue
                    main_family = (inst.data.get("family") or "").lower()
                    if main_family:
                        families.add(main_family)
                    for sub_family in inst.data.get("families", []) or []:
                        sub_family_l = (sub_family or "").lower()
                        if sub_family_l:
                            families.add(sub_family_l)

                # Approve if ANY requirement matches any published family.
                if family_requirements:
                    allow_status_change = False
                    for family_requirement in family_requirements:
                        condition_equal = (
                            family_requirement["condition"] == "equal"
                        )

                        # Support both keys: prefer 'product_type' (current),
                        # fallback to 'family' (legacy)
                        requirement_value = (
                            family_requirement.get("product_type")
                            or family_requirement.get("family")
                            or ""
                        ).lower()

                        if condition_equal:
                            if requirement_value in families:
                                allow_status_change = True
                                break
                        else:
                            if requirement_value not in families:
                                allow_status_change = True
                                break

            # Set note status
            kitsu_status = None
            if self.set_status_note and allow_status_change:
                kitsu_status = gazu.task.get_task_status_by_short_name(
                    self.note_status_shortname
                )
                if kitsu_status:
                    note_status = kitsu_status
                    self.log.info(f"Note Kitsu status: {note_status}")
                else:
                    self.log.info(
                        f"Cannot find {self.note_status_shortname} status."
                        " The status will not be changed!"
                    )

            # Get comment text body
            if is_grouped:
                # Build grouped comment using shared util to match template
                from ayon_kitsu.utils import render_kitsu_comment

                version = (
                    instances[0].data.get("kitsuGroupedVersion")
                    or instances[0].data.get("version", 1)
                )
                names = ", ".join(i.data.get("productName", "Untitled") for i in instances)
                # Use the actual user's comment from the first instance
                user_comment = instances[0].data.get("comment", "")
                data_map = {"comment": user_comment, "version": version, "family": "render", "name": names}
                publish_comment = render_kitsu_comment(self.custom_comment_template, data_map)
            else:
                instance = instances[0]
                publish_comment = instance.data.get("comment")
                if self.custom_comment_template["enabled"]:
                    publish_comment = self.format_publish_comment(instance)

            if not publish_comment:
                self.log.debug("Comment is not set.")
            else:
                self.log.debug(f"Comment is `{publish_comment}`")

            # get the current user
            current_user = gazu.client.get_current_user()

            self.log.info(f"Using note_status ID for comment: {note_status}")
            self.log.info(f"publish_comment: {publish_comment}")
            self.log.info(f"kitsu_task: {kitsu_task}")
            self.log.info(f"kitsu_status: {kitsu_status}")
            self.log.info(f"current_user: {current_user}")
            self.log.info(f"instance: {instance}")
            self.log.info(f"context: {context}")

            # Add comment to kitsu task
            self.log.debug(f"Add new note in tasks id {kitsu_task['id']}")
            try:
                kitsu_comment = gazu.task.add_comment(
                    kitsu_task,
                    note_status,
                    comment=publish_comment,
                    person=current_user,
                )

                # Save the same comment on all instances for this task (grouped or single)
                for inst in instances:
                    inst.data["kitsuComment"] = kitsu_comment
                    # Mark grouped processed to signal preview uploads go to this comment
                    if is_grouped:
                        inst.data["kitsuGroupedReviewProcessed"] = True
            except Exception as e:
                self.log.error(f"Error adding comment to kitsu task: {e}")
                self.log.error(traceback.format_exc())

