# -*- coding: utf-8 -*-
import os
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

    def _get_unique_sprites(self, instance):
        """Get uniqueSprites with standardized priority and debug logging.
        
        Priority order for renderlayer/review instances:
        1. maxUniqueSprites from versionData (aggregated from productGroup)
        2. uniqueSprites from instance data
        3. uniqueSprites from versionData
        4. uniqueSprites from versionEntity
        
        This ensures review instances from productGroups get the max value.
        """
        bundle_name = os.getenv("AYON_BUNDLE_NAME", "Unknown")
        sources_tried = []
        product_type = instance.data.get("productType", "")

        # Priority 1: Aggregated max for renderlayers and reviews (from productGroup aggregation)
        if product_type in ["renderlayer", "review"]:
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

        self.log.warning(
            f"[{bundle_name}] [KitsuComment] No uniqueSprites found for {product_type} instance "
            f"{instance.data.get('productName', 'Unknown')} - tried sources: {sources_tried}"
        )
        return None

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
        # Group instances that are review+kitsu by task id
        by_task = {}
        for instance in context:
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
            # Create individual comments for each instance to link AYON Version to Kitsu Revision
            from ayon_kitsu.utils import render_kitsu_comment

            # get the current user
            current_user = gazu.client.get_current_user()

            self.log.debug(f"Using note_status ID for comment: {note_status}")
            self.log.debug(f"kitsu_task: {kitsu_task}")
            self.log.debug(f"kitsu_status: {kitsu_status}")
            self.log.debug(f"current_user: {current_user}")

                # Create individual comment for each instance
            for instance in instances:
                # Get version for this specific instance
                version = instance.data.get("version", 1)
                product_name = instance.data.get("productName", "Untitled")
                user_comment = instance.data.get("comment", "")

                self.log.debug(
                    f"[{bundle_name}] [KitsuComment] Processing instance: {product_name}, version {version}, "
                    f"task_id={kitsu_task.get('id')}"
                )

                # Get uniqueSprites using standardized helper function
                unique_sprites = self._get_unique_sprites(instance)
                if unique_sprites is not None:
                    source_used = instance.data.get("_uniqueSpritesSource", "unknown")
                    self.log.info(
                        f"[{bundle_name}] [KitsuComment] Using uniqueSprites={unique_sprites} "
                        f"(source: {source_used}) for {product_name}"
                    )
                else:
                    # Log detailed debug info about what data is available
                    product_type = instance.data.get("productType", "")
                    version_data = instance.data.get("versionData", {})
                    self.log.warning(
                        f"[{bundle_name}] [KitsuComment] No uniqueSprites found for {product_name} "
                        f"(productType={product_type}). "
                        f"Available versionData keys: {list(version_data.keys())}"
                    )
                    # Check if this is a review instance that should have gotten maxUniqueSprites
                    if product_type == "review" or "review" in instance.data.get("families", []):
                        product_group = instance.data.get("productGroup")
                        self.log.warning(
                            f"[{bundle_name}] [KitsuComment] Review instance {product_name} "
                            f"missing uniqueSprites. productGroup={product_group}. "
                            f"Check if AggregateRenderlayerSprites ran and if renderlayers "
                            f"have uniqueSprites set."
                        )
                    source_used = None

                # Build comment using template
                self.log.debug(
                    f"[KitsuComment] Building comment for {product_name}, "
                    f"template enabled={self.custom_comment_template.get('enabled', False)}"
                )
                if self.custom_comment_template["enabled"]:
                    # Add uniqueSprites to instance.data for template rendering
                    if unique_sprites is not None:
                        instance.data["uniqueSprites"] = str(unique_sprites)
                    publish_comment = self.format_publish_comment(instance)
                    self.log.debug(
                        f"[KitsuComment] Generated comment using custom template for {product_name}"
                    )
                else:
                    # Use simple format
                    data_map = {
                        "comment": user_comment,
                        "version": version,
                        "family": instance.data.get("family", "render"),
                        "name": product_name,
                    }
                    if unique_sprites is not None:
                        data_map["uniqueSprites"] = str(unique_sprites)
                    self.log.debug(
                        f"[KitsuComment] Using fallback format for {product_name}"
                    )
                    publish_comment = render_kitsu_comment(
                        self.custom_comment_template, data_map
                    )

                if not publish_comment:
                    self.log.warning(
                        f"[KitsuComment] Comment is not set for {product_name}, skipping"
                    )
                    continue
                else:
                    # Log comment preview (first 200 chars to avoid huge logs)
                    comment_preview = (
                        publish_comment[:200] + "..."
                        if len(publish_comment) > 200
                        else publish_comment
                    )
                    self.log.debug(
                        f"[KitsuComment] Generated comment for {product_name} (length={len(publish_comment)}): "
                        f"{comment_preview}"
                    )
                    # Check if uniqueSprites is in the comment
                    if unique_sprites is not None:
                        if (
                            "uniqueSprites" in publish_comment
                            or "unique_sprites" in publish_comment
                        ):
                            self.log.debug(
                                f"[KitsuComment] Confirmed uniqueSprites={unique_sprites} is included in comment for {product_name}"
                            )
                        else:
                            self.log.warning(
                                f"[KitsuComment] uniqueSprites={unique_sprites} may not be included in comment for {product_name}"
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
