# -*- coding: utf-8 -*-
"""Integration plugin for grouped Kitsu review submissions.

This plugin handles multiple review renders by grouping them into a single
Kitsu review with multiple previews, instead of creating separate reviews
for each render.
"""

import os

import gazu
import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishContextPlugin
from ayon_kitsu.utils import (
    render_kitsu_comment,
    resolve_feedback_status,
)


class IntegrateKitsuGroupedReview(KitsuPublishContextPlugin):
    """Integrate multiple review renders as a single grouped Kitsu review.

    This plugin processes all review instances in a publish session and groups
    them into a single Kitsu comment with multiple previews, creating a more
    navigable review experience in Kitsu.
    """

    label = "Kitsu Grouped Review"
    order = (
        pyblish.api.IntegratorOrder - 0.5
    )  # Run much earlier than individual integration plugins
    families = ["kitsu"]
    optional = True

    # Allow the same settings injection as IntegrateKitsuNote for comment template
    custom_comment_template = {
        "enabled": False,
        "comment_template": "{comment}",
    }

    def process(self, context):
        """Process all review instances and group them into a single Kitsu review."""

        # Find all review instances that should be grouped
        review_instances = self._get_review_instances(context)

        if not review_instances:
            self.log.debug("No review instances found for grouping")
            return

        if len(review_instances) == 1:
            self.log.debug(
                "Only one review instance found, no grouping needed"
            )
            return

        self.log.info(
            f"Grouping {len(review_instances)} review instances into single Kitsu review"
        )

        # Debug: Log the instances being grouped
        for instance in review_instances:
            families = instance.data.get("families", [])
            self.log.debug(
                f"Grouping instance: {instance.data.get('productName')} with families: {families}"
            )

        # Group instances by task to ensure we only group reviews for the same task
        task_groups = self._group_instances_by_task(review_instances)

        for task_id, instances in task_groups.items():
            if len(instances) > 1:
                # IMMEDIATELY mark all instances as processed to prevent individual plugins
                for instance in instances:
                    instance.data["kitsuGroupedReviewProcessed"] = True
                    instance.data["kitsuComment"] = (
                        None  # Clear any existing comment
                    )
                    self.log.debug(
                        f"Marked {instance.data.get('productName')} as processed by grouped review"
                    )

                # Create the grouped review
                self._create_grouped_review(task_id, instances)
            else:
                self.log.debug(
                    f"Only one instance for task {task_id}, skipping grouping"
                )

    def _get_review_instances(self, context):
        """Get all review instances that should be grouped.

        Returns:
            list: List of review instances
        """
        review_instances = []

        for instance in context:
            # Check if this is a review instance that should be grouped
            families = instance.data.get("families", [])

            # Look for instances that have both "review" and "kitsu" families
            # This includes render instances that have been converted to reviews via "Attach Review"
            if "review" not in families or "kitsu" not in families:
                continue

            # Skip if already processed by other plugins
            if instance.data.get("kitsuGroupedReviewProcessed"):
                continue

            # Check if we have the required Kitsu context
            kitsu_task = instance.data.get("kitsuTask")
            if not kitsu_task:
                continue

            review_instances.append(instance)

        return review_instances

    def _group_instances_by_task(self, instances):
        """Group instances by Kitsu task ID.

        Args:
            instances (list): List of instances to group

        Returns:
            dict: Dictionary mapping task IDs to lists of instances
        """
        task_groups = {}

        for instance in instances:
            kitsu_task = instance.data.get("kitsuTask")
            if not kitsu_task:
                continue

            task_id = kitsu_task["id"]
            if task_id not in task_groups:
                task_groups[task_id] = []
            task_groups[task_id].append(instance)

        return task_groups

    def _create_grouped_review(self, task_id, instances):
        """Create a single grouped review for multiple instances.

        Args:
            task_id (str): Kitsu task ID
            instances (list): List of instances to group
        """
        self.log.info(
            f"Creating grouped review for task {task_id} with {len(instances)} instances"
        )

        # Use the first instance as the primary instance for comment creation
        primary_instance = instances[0]
        kitsu_task = primary_instance.data.get("kitsuTask")

        self.log.debug(f"Kitsu task data: {kitsu_task}")

        # Create a single comment for all reviews - use shared helper
        comment_text = self._build_comment_text(instances)
        self.log.debug(f"Comment text: {comment_text}")

        try:
            # Prefer setting status to 'Feedback' (shared helper)
            task_status = resolve_feedback_status(kitsu_task)

            if not task_status:
                self.log.error(
                    "Could not find any valid task status for comment creation"
                )
                return

            # Create the comment with task status
            comment = gazu.task.add_comment(
                task=kitsu_task,
                task_status=task_status,
                comment=comment_text,
            )
            self.log.info("Created grouped review comment with task status")

            comment_id = comment["id"]
            self.log.info(f"Created grouped review comment: {comment_id}")

            # Store comment info in all instances
            for instance in instances:
                instance.data["kitsuComment"] = comment

        except Exception as exc:
            self.log.error(f"Failed to create grouped review comment: {exc}")
            # Even if comment creation fails, mark instances as processed to prevent individual plugins
            for instance in instances:
                instance.data["kitsuGroupedReviewProcessed"] = True
            return

        # Upload all review files as previews to the same comment
        uploaded_previews = []

        for instance in instances:
            self.log.debug(
                f"Processing instance: {instance.data.get('productName')}"
            )

            # Upload main file - for review instances (including converted renders)
            main_file = instance.data.get("reviewFile")
            self.log.debug(f"reviewFile: {main_file}")

            # If no reviewFile, try to get from representations (fallback for converted renders)
            if not main_file:
                representations = instance.data.get("representations", [])
                self.log.debug(f"Found {len(representations)} representations")

                # Debug: Log all representations
                for i, repr in enumerate(representations):
                    repr_name = repr.get("name", "")
                    repr_tags = repr.get("tags", [])
                    self.log.debug(
                        f"  Repr {i}: name='{repr_name}', tags={repr_tags}"
                    )

                # Look for the main video representation
                for repr in representations:
                    repr_name = repr.get("name", "").lower()
                    repr_tags = repr.get("tags", [])

                    # Check if this is a video representation
                    is_video = any(
                        ext in repr_name
                        for ext in ["mp4", "mov", "avi", "h264", "webm"]
                    )
                    has_kitsureview_tag = "kitsureview" in repr_tags

                    self.log.debug(
                        f"  Checking repr '{repr_name}': is_video={is_video}, "
                        f"has_kitsureview_tag={has_kitsureview_tag}"
                    )

                    if has_kitsureview_tag and is_video:
                        staging_dir = repr.get("stagingDir")
                        files = repr.get("files")
                        if files and staging_dir:
                            if isinstance(files, str):
                                main_file = os.path.join(staging_dir, files)
                            elif isinstance(files, list) and files:
                                main_file = os.path.join(staging_dir, files[0])
                            self.log.debug(
                                f"Found main video file from representation: {main_file}"
                            )
                            break

                # If still no main file, try first representation
                if not main_file and representations:
                    first_repr = representations[0]
                    staging_dir = first_repr.get("stagingDir")
                    files = first_repr.get("files")
                    if files and staging_dir:
                        if isinstance(files, str):
                            main_file = os.path.join(staging_dir, files)
                        elif isinstance(files, list) and files:
                            main_file = os.path.join(staging_dir, files[0])
                        self.log.debug(
                            f"Using first representation file: {main_file}"
                        )

            self.log.debug(f"Final main_file: {main_file}")
            if main_file and os.path.exists(main_file):
                try:
                    # Use synchronized version if available, otherwise fall back to instance version
                    version = instance.data.get(
                        "kitsuGroupedVersion"
                    ) or instance.data.get("version", 1)
                    preview = gazu.task.add_preview(
                        task=task_id,
                        comment=comment_id,
                        preview_file_path=main_file,
                        normalize_movie=False,
                        revision=version,
                    )
                    uploaded_previews.append(
                        {
                            "file": os.path.basename(main_file),
                            "preview_id": preview.get("id"),
                            "instance": instance.data.get(
                                "productName", "Unknown"
                            ),
                        }
                    )
                    self.log.info(
                        f"Uploaded main file: {os.path.basename(main_file)}"
                    )

                except Exception as exc:
                    self.log.warning(
                        f"Failed to upload main file {main_file}: {exc}"
                    )

            # Skip additional representations for grouped reviews to avoid duplicate thumbnails
            # The main file upload above is sufficient for grouped reviews
            self.log.debug(
                f"Skipping additional representations for {instance.data.get('productName')} in grouped review"
            )

        # Store uploaded previews info
        for instance in instances:
            instance.data["kitsuPreview"] = uploaded_previews

        self.log.info(
            f"Successfully created grouped review with {len(uploaded_previews)} previews"
        )

    def _create_grouped_comment_text(self, instances):
        """Create comment text for grouped review.

        Args:
            instances (list): List of instances being grouped

        Returns:
            str: Comment text for the grouped review
        """
        if len(instances) == 1:
            return (
                f"Review: {instances[0].data.get('productName', 'Untitled')}"
            )

        # Create versioned comment text expected by Kitsu UI parser
        # Lines:
        #   version\t{int}
        #   family\trender
        #   name\tcomma,separated,names

        instance_names = [
            inst.data.get("productName", "Untitled") for inst in instances
        ]

        # Use synchronized grouped version if available; fallback to instance version; default 1
        version = instances[0].data.get("kitsuGroupedVersion") or instances[
            0
        ].data.get("version", 1)

        # Always report family as 'render' for attached reviewables from renders
        comment_text = (
            f"version\t{version}\n"
            f"family\trender\n"
            f"name\t{', '.join(instance_names)}"
        )

        return comment_text

    def _build_comment_text(self, instances):
        """Build comment text using the same template as single render notes.

        If the template is enabled in settings, render it with instance data from
        the first instance (they share the same version/family/name context). If
        not enabled, fall back to the versioned tab-delimited comment format.
        """
        # Compose a data map for comment rendering
        first = instances[0]
        version = (
            first.data.get("kitsuGroupedVersion") or first.data.get("version", 1)
        )
        product_names = ", ".join(
            inst.data.get("productName", "Untitled") for inst in instances
        )
        grouped_line = (
            "Grouped Review: " + product_names if product_names else "Grouped Review"
        )
        data_map = {"comment": grouped_line, "version": version, "family": "render", "name": product_names}

        # Use shared helper which mirrors IntegrateKitsuNote behavior
        return render_kitsu_comment(self.custom_comment_template, data_map)
