# -*- coding: utf-8 -*-
import gazu
import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishInstancePlugin


class IntegrateKitsuReview(KitsuPublishInstancePlugin):
    """Integrate Kitsu Review"""

    order = pyblish.api.IntegratorOrder + 0.01
    label = "Kitsu Review"
    families = ["kitsu"]
    optional = True

    def process(self, instance):
        product_name = instance.data.get("productName", "Unknown")
        families = instance.data.get("families", [])

        self.log.debug(
            f"IntegrateKitsuReview processing {product_name} with families: {families}"
        )

        # Do not skip here; if a grouped note created a comment, we still
        # want to upload previews to that comment.

        # Check comment has been created
        comment_id = instance.data.get("kitsuComment", {}).get("id")
        if not comment_id:
            self.log.debug(
                "Comment not created, review not pushed to preview."
            )
            return

        kitsu_task = instance.data.get("kitsuTask")
        if not kitsu_task:
            self.log.debug("No kitsu task found, skipping review upload.")
            return

        # Add review representations as preview of comment
        task_id = kitsu_task["id"]
        for representation in instance.data.get("representations", []):
            # Skip if not tagged as review
            if "kitsureview" not in representation.get("tags", []):
                self.log.debug(
                    f"Skipping representation {representation['name']} "
                    "because it has no 'kitsureview' tag"
                )
                continue
            review_path = representation.get("published_path")
            self.log.debug(f"Found review at: {review_path}")

            # Working around normalization failure with retry logic
            max_retries = 2
            retry_count = 0
            upload_successful = False

            while retry_count < max_retries and not upload_successful:
                try:
                    gazu.task.add_preview(
                        task=task_id,
                        comment=comment_id,
                        preview_file_path=review_path,
                        normalize_movie=False,
                        revision=instance.data["version"],
                    )
                    self.log.info("Review upload successful on comment")
                    upload_successful = True
                except gazu.exception.ParameterException as e:
                    retry_count += 1
                    if retry_count < max_retries:
                        self.log.warning(
                            "Normalization failed, retrying upload attempt "
                            f"{retry_count}/{max_retries}: {e}"
                        )
                    else:
                        self.log.error(
                            f"Failed to upload review after {max_retries} "
                            f"attempts: {e}"
                        )
                        raise Exception(
                            f"Failed to upload review after {max_retries} "
                            f"attempts: {e}"
                        )
                except Exception as e:
                    self.log.error(f"Failed to upload review: {e}")
                    raise Exception(f"Failed to upload review: {e}")
