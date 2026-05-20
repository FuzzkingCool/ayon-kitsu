# -*- coding: utf-8 -*-
import os
import re
import traceback

import gazu
import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishContextPlugin
from ayon_kitsu.utils import format_kitsu_task_display


def _kitsu_note_task_field(task_entity):
    """Value for Kitsu note ``task_name`` when AYON task slug differs from task type.

    Returns ``name ( taskType )`` only when ``name`` and ``taskType`` name differ
    case-insensitively; otherwise ``None``.
    """
    if not task_entity or not isinstance(task_entity, dict):
        return None
    task_name = (task_entity.get("name") or "").strip()
    tt = task_entity.get("taskType")
    if isinstance(tt, dict):
        task_type_name = (tt.get("name") or "").strip()
    elif isinstance(tt, str):
        task_type_name = tt.strip()
    else:
        task_type_name = ""
    if not task_name or not task_type_name:
        return None
    if task_name.lower() == task_type_name.lower():
        return None
    return f"{task_name} ( {task_type_name} )"


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

    def _unique_sprites_applies(self, context, instance):
        """True when Harmony uniqueSprites should appear on Kitsu notes / persist to version.

        Includes ``review`` because Kitsu comments are created for review instances that
        carry ``versionData.maxUniqueSprites`` aggregated from renderlayers (see Harmony
        AggregateRenderlayerSprites). Render/renderlayer products are included when those
        instances are processed directly.
        """
        host = (
            context.data.get("hostName")
            or os.environ.get("AYON_HOST_NAME")
            or ""
        )
        if str(host).lower() != "harmony":
            return False
        product_type = instance.data.get("productType") or ""
        return product_type in ("render", "renderlayer", "review")

    def _get_unique_sprites(self, instance):
        """Get uniqueSprites with standardized priority and debug logging.

        Priority order for Harmony render / renderlayer / review instances:
        1. maxUniqueSprites from versionData (aggregated from productGroup; review uses this)
        2. uniqueSprites from instance data
        3. uniqueSprites from versionData
        4. uniqueSprites from versionEntity
        """
        bundle_name = os.getenv("AYON_BUNDLE_NAME", "Unknown")
        sources_tried = []
        product_type = instance.data.get("productType", "")

        # Priority 1: Aggregated max from productGroup (render, renderlayer, review)
        if product_type in ("render", "renderlayer", "review"):
            max_sprites = instance.data.get("versionData", {}).get("maxUniqueSprites")
            sources_tried.append("versionData.maxUniqueSprites")
            if max_sprites is not None:
                instance.data["_uniqueSpritesSource"] = "aggregated_max"
                self.log.info(
                    f"[{bundle_name}] [KitsuComment] Using aggregated max uniqueSprites={max_sprites} "
                    f"for {product_type} instance {instance.data.get('productName', 'Unknown')}"
                )
                return max_sprites

        # Priority 2: Instance data
        sprites = instance.data.get("uniqueSprites")
        sources_tried.append("instance.data.uniqueSprites")
        if sprites is not None:
            instance.data["_uniqueSpritesSource"] = "instance_data"
            self.log.debug(
                f"[{bundle_name}] [KitsuComment] Using instance uniqueSprites={sprites}"
            )
            return sprites

        # Priority 3: Version data
        sprites = instance.data.get("versionData", {}).get("uniqueSprites")
        sources_tried.append("versionData.uniqueSprites")
        if sprites is not None:
            instance.data["_uniqueSpritesSource"] = "version_data"
            self.log.debug(
                f"[{bundle_name}] [KitsuComment] Using versionData uniqueSprites={sprites}"
            )
            return sprites

        # Priority 4: Version entity
        if instance.data.get("versionEntity"):
            sprites = instance.data["versionEntity"].get("data", {}).get("uniqueSprites")
            sources_tried.append("versionEntity.data.uniqueSprites")
            if sprites is not None:
                instance.data["_uniqueSpritesSource"] = "version_entity"
                self.log.debug(
                    f"[{bundle_name}] [KitsuComment] Using versionEntity uniqueSprites={sprites}"
                )
                return sprites

        msg = (
            f"[{bundle_name}] [KitsuComment] No uniqueSprites found for {product_type} instance "
            f"{instance.data.get('productName', 'Unknown')} - tried sources: {sources_tried}"
        )
        if product_type in ("render", "renderlayer"):
            self.log.warning(msg)
        else:
            self.log.debug(msg)
        return None

    def _persist_unique_sprites_to_version(self, context, instance, unique_sprites):
        """Write uniqueSprites to the AYON version entity so status-change handler can read it."""
        import ayon_api

        project_name = context.data.get("projectName")
        version_entity = instance.data.get("versionEntity")
        if not project_name or not version_entity:
            return
        version_id = version_entity.get("id")
        if not version_id:
            return
        try:
            data = dict(version_entity.get("data") or {})
            data["uniqueSprites"] = str(unique_sprites)
            ayon_api.update_version(project_name, version_id, data=data)
            self.log.debug(
                f"[KitsuComment] Persisted uniqueSprites={unique_sprites} to version {version_id}"
            )
        except Exception as e:
            self.log.warning(
                f"[KitsuComment] Failed to persist uniqueSprites to version: {e}"
            )

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
            if key in ("task", "task_name"):
                return format_kitsu_task_display(instance.data[key])
            return str(instance.data[key])

        template = self.custom_comment_template["comment_template"]
        pattern = r"\{([^}]*)\}"
        result = re.sub(pattern, replace_missing_key, template)
        # Omit uniqueSprites line when value is 0 or empty (tab or table template format)
        if str(instance.data.get("uniqueSprites", "")).strip() in ("", "0"):
            result = re.sub(r"\n[^\n]*uniqueSprites[^\n]*", "", result)
        return result

    def process(self, context):
        bundle_name = os.getenv("AYON_BUNDLE_NAME", "Unknown")

        if "product_type_requirements" in self.status_change_conditions:
            family_requirements = self.status_change_conditions[
                "product_type_requirements"
            ]
        else:
            family_requirements = self.status_change_conditions[
                "family_requirements"
            ]

        # Detect grouped vs single review per Kitsu task
        # Group instances that are review+kitsu by task id (skip merged-away instances)
        by_task = {}
        for instance in context:
            if instance.data.get("kitsuMergedInto"):
                continue
            families = set(
                [instance.data["family"]] + instance.data.get("families", [])
            )
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

                        # Support both keys: prefer 'product_type', fallback to 'family'
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
                    self.log.debug(f"Note Kitsu status: {note_status}")
                else:
                    self.log.debug(
                        f"Cannot find {self.note_status_shortname} status."
                        " The status will not be changed!"
                    )

            # Get comment text body
            from ayon_kitsu.utils import render_kitsu_comment

            current_user = gazu.client.get_current_user()

            self.log.debug(f"Using note_status ID for comment: {note_status}")
            self.log.debug(f"kitsu_task: {kitsu_task}")
            self.log.debug(f"kitsu_status: {kitsu_status}")
            self.log.debug(f"current_user: {current_user}")

            # One comment per task when multiple review instances share the task
            if is_grouped:
                product_names = [inst.data.get("productName", "Untitled") for inst in instances]
                first_instance = instances[0]
                first_version = first_instance.data.get("version", 1)
                kitsu_only_group = any(inst.data.get("kitsuOnlyReview", False) for inst in instances)
                combined_unique_sprites = None
                if not kitsu_only_group:
                    for instance in instances:
                        if not self._unique_sprites_applies(context, instance):
                            continue
                        unique_sprites = self._get_unique_sprites(instance)
                        if unique_sprites is not None and str(unique_sprites).strip() not in ("", "0"):
                            self._persist_unique_sprites_to_version(context, instance, unique_sprites)
                            combined_unique_sprites = unique_sprites
                combined_name = ", ".join(product_names)
                data_map = {
                    "comment": first_instance.data.get("comment", ""),
                    "version": first_version,
                    "family": first_instance.data.get("family", "review"),
                    "name": combined_name,
                }
                task_note = _kitsu_note_task_field(
                    first_instance.data.get("taskEntity")
                )
                if task_note:
                    data_map["task_name"] = task_note
                if (
                    combined_unique_sprites is not None
                    and str(combined_unique_sprites).strip() not in ("", "0")
                ):
                    data_map["uniqueSprites"] = str(combined_unique_sprites)
                publish_comment = render_kitsu_comment(
                    self.custom_comment_template, data_map
                )
                if not publish_comment:
                    publish_comment = f"Review: {combined_name}"
                try:
                    kitsu_comment = gazu.task.add_comment(
                        kitsu_task,
                        note_status,
                        comment=publish_comment,
                        person=current_user,
                    )
                    comment_id = kitsu_comment.get("id") if isinstance(kitsu_comment, dict) else None
                    for instance in instances:
                        instance.data["kitsuComment"] = kitsu_comment
                        instance.data["kitsuGroupedReviewProcessed"] = True
                    self.log.debug(
                        f"[{bundle_name}] [KitsuComment] Created single comment (id={comment_id}) "
                        f"for task, {len(instances)} instance(s): {combined_name}"
                    )
                except Exception as e:
                    self.log.error(
                        f"[KitsuComment] Error adding grouped comment to Kitsu task: {e}"
                    )
                    self.log.error(traceback.format_exc())
                continue

            # Create individual comment for each instance
            for instance in instances:
                version = instance.data.get("version", 1)
                product_name = instance.data.get("productName", "Untitled")
                user_comment = instance.data.get("comment", "")
                kitsu_only = instance.data.get("kitsuOnlyReview", False)

                self.log.debug(
                    f"[{bundle_name}] [KitsuComment] Processing instance: {product_name}, version {version}, "
                    f"task_id={kitsu_task.get('id')}"
                )

                if kitsu_only or not self._unique_sprites_applies(context, instance):
                    # Kitsu-only review, or non-Harmony / non-applicable product: no uniqueSprites
                    data_map = {
                        "comment": user_comment,
                        "version": version,
                        "family": instance.data.get("family", "review"),
                        "name": product_name,
                    }
                    task_note = _kitsu_note_task_field(
                        instance.data.get("taskEntity")
                    )
                    if task_note:
                        data_map["task_name"] = task_note
                        instance.data["task_name"] = task_note
                    else:
                        instance.data.pop("task_name", None)
                    publish_comment = render_kitsu_comment(
                        self.custom_comment_template, data_map
                    )
                    if not publish_comment:
                        publish_comment = f"Review: {product_name}"
                    unique_sprites = None
                else:
                    # Harmony render / renderlayer / review: uniqueSprites and optional template
                    unique_sprites = self._get_unique_sprites(instance)
                    has_unique_sprites = (
                        unique_sprites is not None
                        and str(unique_sprites).strip() not in ("", "0")
                    )
                    if has_unique_sprites:
                        self._persist_unique_sprites_to_version(context, instance, unique_sprites)
                        if self.custom_comment_template["enabled"]:
                            instance.data["uniqueSprites"] = str(unique_sprites)
                    else:
                        product_type = instance.data.get("productType", "")
                        creator_attributes = (
                            instance.data.get("creator_attributes") or {}
                        )
                        count_unique_sprites = creator_attributes.get(
                            "count_unique_sprites", True
                        )
                        if unique_sprites is not None or not count_unique_sprites:
                            self.log.debug(
                                f"[{bundle_name}] [KitsuComment] Skipping uniqueSprites "
                                f"annotation for {product_name} "
                                f"(count_unique_sprites={count_unique_sprites}, "
                                f"value={unique_sprites!r})"
                            )
                        elif product_type in ("render", "renderlayer"):
                            version_data = instance.data.get("versionData", {})
                            self.log.warning(
                                f"[{bundle_name}] [KitsuComment] uniqueSprites is "
                                f"missing for {product_name} (productType={product_type}). "
                                f"To get a count: enable 'Count Unique Sprites' on the "
                                f"render/renderlayer creator. If layered, also confirm "
                                f"AggregateRenderlayerSprites ran. "
                                f"versionData keys: {list(version_data.keys())}"
                            )
                        else:
                            version_data = instance.data.get("versionData", {})
                            self.log.debug(
                                f"[{bundle_name}] [KitsuComment] uniqueSprites missing "
                                f"for {product_name} (productType={product_type}); "
                                f"versionData keys: {list(version_data.keys())}"
                            )

                    if self.custom_comment_template["enabled"]:
                        task_note = _kitsu_note_task_field(
                            instance.data.get("taskEntity")
                        )
                        if task_note:
                            instance.data["task_name"] = task_note
                        else:
                            instance.data.pop("task_name", None)
                        publish_comment = self.format_publish_comment(instance)
                    else:
                        data_map = {
                            "comment": user_comment,
                            "version": version,
                            "family": instance.data.get("family", "render"),
                            "name": product_name,
                        }
                        if has_unique_sprites:
                            data_map["uniqueSprites"] = str(unique_sprites)
                        task_note = _kitsu_note_task_field(
                            instance.data.get("taskEntity")
                        )
                        if task_note:
                            data_map["task_name"] = task_note
                            instance.data["task_name"] = task_note
                        else:
                            instance.data.pop("task_name", None)
                        publish_comment = render_kitsu_comment(
                            self.custom_comment_template, data_map
                        )

                    if not publish_comment:
                        publish_comment = f"Review: {product_name}"
                        self.log.debug(
                            f"[KitsuComment] Empty comment after template for {product_name}; "
                            f"using fallback so Kitsu review can attach"
                        )

                # Add individual comment to kitsu task for this instance
                self.log.debug(
                    f"[KitsuComment] Posting comment to Kitsu task_id={kitsu_task['id']} "
                    f"for {product_name} version {version}, note_status_id={note_status}"
                )
                try:
                    kitsu_comment = gazu.task.add_comment(
                        kitsu_task,
                        note_status,
                        comment=publish_comment,
                        person=current_user,
                    )

                    # Save the comment on this specific instance
                    instance.data["kitsuComment"] = kitsu_comment
                    comment_id = (
                        kitsu_comment.get("id")
                        if isinstance(kitsu_comment, dict)
                        else None
                    )

                    # Mark grouped processed to signal preview uploads go to this comment
                    if is_grouped:
                        instance.data["kitsuGroupedReviewProcessed"] = True

                    self.log.debug(
                        f"[{bundle_name}] [KitsuComment] Successfully created Kitsu comment (id={comment_id}) "
                        f"for {product_name} version {version} "
                        f"with uniqueSprites={unique_sprites}"
                    )
                except Exception as e:
                    self.log.error(
                        f"[KitsuComment] Error adding comment to kitsu task for {product_name}: {e}"
                    )
                    self.log.error(traceback.format_exc())
