# -*- coding: utf-8 -*-
"""Integration plugin for Kitsu-only review submissions.

This plugin handles review instances that should only be submitted to Kitsu
without creating any AYON representations or versions.
"""

import os

import gazu
import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishInstancePlugin


class IntegrateKitsuOnlyReview(KitsuPublishInstancePlugin):
    """Integrate review directly to Kitsu without AYON publishing.

    This plugin bypasses the normal AYON publishing pipeline and submits
    reviews directly to Kitsu. It's designed for the Kitsu Review creator
    that allows artists to submit reviews without creating AYON products.
    """

    label = "Kitsu Only Review"
    order = pyblish.api.IntegratorOrder + 0.02
    families = ["kitsu"]
    optional = True

    def process(self, instance):
        """Process the review instance for Kitsu-only submission."""

        # Skip if this instance is being processed by grouped review plugin
        if instance.data.get("kitsuGroupedReviewProcessed"):
            self.log.debug(
                f"Instance {instance.data.get('productName')} "
                "already processed by grouped review plugin, skipping"
            )
            return

        # Also skip if this instance has the "review" family 
        # (should be handled by grouped plugin)
        families = instance.data.get("families", [])
        if "review" in families and "kitsu" in families:
            self.log.debug(
                f"Instance {instance.data.get('productName')} "
                "has review family, will be handled by grouped plugin, skipping"
            )
            return

        # Skip if this is a render instance that has been converted to review
        if (
            "render" in families
            and "review" in families
            and "kitsu" in families
        ):
            self.log.debug(
                f"Instance {instance.data.get('productName')}"
                 "is a render converted to review, should be handled by "
                 "grouped plugin, skipping"
            )
            return

        # Only process instances marked as Kitsu-only reviews
        if not instance.data.get("kitsuOnlyReview", False):
            self.log.debug("Not a Kitsu-only review, skipping")
            return

        # Check if we have required Kitsu context
        kitsu_task = instance.data.get("kitsuTask")
        if not kitsu_task:
            self.log.warning("No Kitsu task found, cannot submit review")
            return

        task_id = kitsu_task["id"]
        self.log.info(f"Processing Kitsu-only review for task: {task_id}")

        # Get review file from instance data
        review_file = instance.data.get("reviewFile")
        if not review_file or not os.path.exists(review_file):
            self.log.error(f"Review file not found: {review_file}")
            raise ValueError(f"Review file not found: {review_file}")

        # Use existing comment from integrate_kitsu_note.py if available
        comment_id = None
        kitsu_comment = instance.data.get("kitsuComment")
        if kitsu_comment:
            comment_id = kitsu_comment.get("id")
            self.log.info(f"Using existing comment: {comment_id}")

        # If no comment exists, create a minimal one
        if not comment_id:
            try:
                comment_text = (
                    f"Review: {instance.data.get('productName', 'Untitled')}"
                )
                comment = gazu.task.add_comment(
                    task=kitsu_task,
                    task_status=gazu.task.get_task_status_by_name("wip"),
                    comment=comment_text,
                )
                comment_id = comment["id"]
                self.log.info(f"Created fallback comment: {comment_text}")

                # Store comment info
                instance.data["kitsuComment"] = comment

            except Exception as exc:
                self.log.error(f"Failed to create fallback comment: {exc}")
                # Continue without comment if comment creation fails

        # Upload review as preview (main review file)
        try:
            # Get version number (default to 1 for reviews)
            version = instance.data.get("version", 1)

            if comment_id:
                # Add main review preview to the comment
                preview = gazu.task.add_preview(
                    task=task_id,
                    comment=comment_id,
                    preview_file_path=review_file,
                    normalize_movie=False,
                    revision=version,
                )
                self.log.info(
                    f"Uploaded review to Kitsu with comment: {os.path.basename(review_file)}"
                )
            else:
                # Upload preview without comment
                # Create a minimal comment first
                comment = gazu.task.add_comment(
                    task=kitsu_task,
                    task_status=gazu.task.get_task_status_by_name("wip"),
                    comment="Review submission",
                )
                comment_id = comment["id"]

                preview = gazu.task.add_preview(
                    task=task_id,
                    comment=comment_id,
                    preview_file_path=review_file,
                    normalize_movie=False,
                    revision=version,
                )
                self.log.info(
                    f"Uploaded review to Kitsu: {os.path.basename(review_file)}"
                )

            # Store preview info
            instance.data["kitsuPreview"] = preview

            # Upload additional representations (like screenshots/thumbnails)
            representations = instance.data.get("representations", [])
            for representation in representations:
                if "kitsureview" in representation.get("tags", []):
                    repr_files = representation.get("files")
                    staging_dir = representation.get("stagingDir")

                    if isinstance(repr_files, str):
                        repr_files = [repr_files]

                    # Track uploaded previews for logging and potential future use
                    uploaded_previews = []

                    for repr_file in repr_files:
                        repr_path = os.path.join(staging_dir, repr_file)
                        if os.path.exists(repr_path):
                            try:
                                # Upload additional preview (e.g., screenshot/thumbnail)
                                additional_preview = gazu.task.add_preview(
                                    task=task_id,
                                    comment=comment_id,
                                    preview_file_path=repr_path,
                                    normalize_movie=False,
                                    revision=version,
                                )

                                # Store preview information for potential future use
                                preview_info = {
                                    "file": repr_file,
                                    "preview_id": additional_preview.get("id")
                                    if additional_preview
                                    else None,
                                    "preview_data": additional_preview,
                                }
                                uploaded_previews.append(preview_info)

                                self.log.info(
                                    f"Uploaded additional review item to Kitsu: {repr_file}"
                                )
                                if (
                                    additional_preview
                                    and additional_preview.get("id")
                                ):
                                    self.log.debug(
                                        f"Preview ID: {additional_preview['id']}"
                                    )

                            except Exception as exc:
                                self.log.warning(
                                    f"Failed to upload additional review item {repr_file}: {exc}"
                                )
                        else:
                            self.log.warning(
                                f"Additional review file not found: {repr_path}"
                            )

                    # Store uploaded previews in instance data for potential future use
                    if uploaded_previews:
                        instance.data["uploadedKitsuPreviews"] = (
                            uploaded_previews
                        )
                        self.log.info(
                            f"Successfully uploaded {len(uploaded_previews)} additional preview(s) to Kitsu"
                        )

        except Exception as exc:
            self.log.error(f"Failed to upload review to Kitsu: {exc}")
            raise RuntimeError(f"Failed to upload review to Kitsu: {exc}")

        # Update task status if requested
        if instance.data.get("setReviewStatus", False):
            try:
                # Set task status to "waiting for approval" or similar
                review_status = gazu.task.get_task_status_by_name(
                    "waiting for approval"
                )
                if review_status:
                    gazu.task.update_task(
                        kitsu_task, {"task_status_id": review_status["id"]}
                    )
                    self.log.info(
                        "Updated task status to 'waiting for approval'"
                    )
                else:
                    self.log.warning(
                        "Could not find 'waiting for approval' status"
                    )

            except Exception as exc:
                self.log.warning(f"Failed to update task status: {exc}")
                # Don't fail the whole process if status update fails

        # Mark instance as processed for Kitsu-only workflow
        instance.data["kitsuOnlyProcessed"] = True

        self.log.info("Successfully processed Kitsu-only review submission")
