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

        if instance.data.get("kitsuMergedInto"):
            self.log.debug(
                f"Instance {instance.data.get('productName')} was merged into another, skipping"
            )
            return

        # When kitsuGroupedReviewProcessed is set, a single comment was created
        # for the task; we still run to upload this instance's previews to it.

        # Skip "review" + "kitsu" only when NOT kitsuOnlyReview (e.g. Harmony
        # render+review uses IntegrateKitsuReview with published_path).
        families = instance.data.get("families", [])
        if "review" in families and "kitsu" in families:
            if not instance.data.get("kitsuOnlyReview", False):
                self.log.debug(
                    f"Instance {instance.data.get('productName')} "
                    "has review family, will be handled by IntegrateKitsuReview, skipping"
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

        # Upload review as preview (main + additional files)
        try:
            version = instance.data.get("version", 1)

            # Prefer explicit list from creator; fallback to representations
            review_file_paths = instance.data.get("reviewFilePaths") or []
            if not review_file_paths:
                representations = instance.data.get("representations", [])
                if not representations and getattr(instance, "transient_data", None):
                    representations = instance.transient_data.get("representations", [])
                for representation in representations:
                    if "kitsureview" not in representation.get("tags", []):
                        continue
                    staging_dir = representation.get("stagingDir") or representation.get("staging_dir") or ""
                    if not staging_dir:
                        continue
                    repr_files = representation.get("files")
                    if isinstance(repr_files, str):
                        repr_files = [repr_files]
                    for repr_file in repr_files or []:
                        if repr_file:
                            review_file_paths.append(os.path.normpath(os.path.join(staging_dir, repr_file)))

            # Dedupe while preserving order; ensure main review file is first if set
            seen = set()
            ordered_paths = []
            if review_file and os.path.exists(review_file):
                rn = os.path.normpath(review_file)
                if rn not in seen:
                    seen.add(rn)
                    ordered_paths.append(rn)
            for p in review_file_paths:
                pn = os.path.normpath(p)
                if pn not in seen and os.path.exists(pn):
                    seen.add(pn)
                    ordered_paths.append(pn)
            if not ordered_paths:
                self.log.error("No review files found to upload")
                raise ValueError("No review files found to upload")

            main_path = ordered_paths[0]
            if comment_id:
                preview = gazu.task.add_preview(
                    task=task_id,
                    comment=comment_id,
                    preview_file_path=main_path,
                    normalize_movie=False,
                    revision=version,
                )
                self.log.info(f"Uploaded review to Kitsu: {os.path.basename(main_path)}")
            else:
                comment = gazu.task.add_comment(
                    task=kitsu_task,
                    task_status=gazu.task.get_task_status_by_name("wip"),
                    comment="Review submission",
                )
                comment_id = comment["id"]
                preview = gazu.task.add_preview(
                    task=task_id,
                    comment=comment_id,
                    preview_file_path=main_path,
                    normalize_movie=False,
                    revision=version,
                )
                self.log.info(f"Uploaded review to Kitsu: {os.path.basename(main_path)}")

            instance.data["kitsuPreview"] = preview
            uploaded_previews = []

            for path in ordered_paths[1:]:
                if not os.path.exists(path):
                    self.log.warning(f"Review file not found: {path}")
                    continue
                try:
                    additional_preview = gazu.task.add_preview(
                        task=task_id,
                        comment=comment_id,
                        preview_file_path=path,
                        normalize_movie=False,
                        revision=version,
                    )
                    uploaded_previews.append({
                        "file": os.path.basename(path),
                        "preview_id": additional_preview.get("id") if additional_preview else None,
                        "preview_data": additional_preview,
                    })
                    self.log.info(f"Uploaded review item to Kitsu: {os.path.basename(path)}")
                except Exception as exc:
                    self.log.warning(f"Failed to upload review item {path}: {exc}")

            if uploaded_previews:
                instance.data["uploadedKitsuPreviews"] = (
                    instance.data.get("uploadedKitsuPreviews") or []
                ) + uploaded_previews
            self.log.info(
                f"Uploaded {1 + len(uploaded_previews)} item(s) to Kitsu revision"
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
