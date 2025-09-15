# -*- coding: utf-8 -*-
"""Validation plugin for Kitsu-only review instances.

This plugin validates that Kitsu-only review instances have all the
required data for successful submission to Kitsu.
"""

import os

from ayon_core.pipeline.publish import (
    OptionalPyblishPluginMixin,
    PublishValidationError,
    ValidateContentsOrder,
)
from ayon_kitsu.pipeline import KitsuPublishInstancePlugin


class ValidateKitsuOnlyReview(
    OptionalPyblishPluginMixin, KitsuPublishInstancePlugin
):
    """Validate Kitsu-only review instances.

    Ensures that instances marked as Kitsu-only reviews have:
    - Valid review file path
    - Required Kitsu context (task info)
    - Proper representation data
    """

    label = "Validate Kitsu Only Review"
    order = ValidateContentsOrder + 0.1
    families = ["kitsu_only"]
    optional = True

    def process(self, instance):
        """Validate the Kitsu-only review instance."""

        # Only validate instances marked as Kitsu-only reviews
        if not instance.data.get("kitsuOnlyReview", False):
            return

        self.log.info(
            f"Validating Kitsu-only review: {instance.data.get('productName', 'Unnamed')}"
        )

        errors = []

        # Validate review file exists
        review_file = instance.data.get("reviewFile")
        if not review_file:
            errors.append("No review file specified")
        elif not os.path.exists(review_file):
            errors.append(f"Review file does not exist: {review_file}")
        elif not os.path.isfile(review_file):
            errors.append(f"Review path is not a file: {review_file}")
        else:
            # Check file size (should not be empty)
            file_size = os.path.getsize(review_file)
            if file_size == 0:
                errors.append(f"Review file is empty: {review_file}")
            else:
                self.log.debug(
                    f"Review file validated: {review_file} ({file_size} bytes)"
                )

        # Validate Kitsu context
        kitsu_task = instance.data.get("kitsuTask")
        if not kitsu_task:
            errors.append(
                "No Kitsu task context found. Ensure you are working in a valid AYON project with Kitsu integration."
            )
        else:
            # Validate task has required fields
            task_id = kitsu_task.get("id")
            if not task_id:
                errors.append("Kitsu task is missing ID")
            else:
                self.log.debug(f"Kitsu task validated: {task_id}")

        # Validate representations (optional for Kitsu-only reviews)
        representations = instance.data.get("representations", [])
        if representations:
            # If representations exist, validate them
            for i, representation in enumerate(representations):
                repr_errors = self._validate_representation(representation, i)
                errors.extend(repr_errors)
        else:
            # For Kitsu-only reviews, representations are optional since we upload directly
            self.log.debug(
                "No representations found - this is expected for Kitsu-only reviews"
            )

        # Validate required instance data
        required_fields = ["productName", "productType", "folderPath", "task"]
        for field in required_fields:
            if not instance.data.get(field):
                errors.append(f"Missing required field: {field}")

        # Check families
        families = instance.data.get("families", [])
        if "kitsu" not in families:
            errors.append(
                "Instance must have 'kitsu' family for Kitsu integration"
            )

        # Validate comment if enabled
        if instance.data.get("enableComment", True):
            comment = instance.data.get("comment", "")
            if not comment.strip():
                # This is just a warning, not an error
                self.log.warning(
                    "Comment is enabled but no comment text provided. A default comment will be used."
                )

        # Report validation results
        if errors:
            error_msg = "Kitsu-only review validation failed:\n" + "\n".join(
                f"- {error}" for error in errors
            )
            self.log.error(error_msg)
            raise PublishValidationError(error_msg)
        else:
            self.log.info("Kitsu-only review validation passed")

    def _validate_representation(self, representation, index):
        """Validate a single representation.

        Args:
            representation (dict): Representation data
            index (int): Index of the representation for error reporting

        Returns:
            list: List of validation errors
        """
        errors = []

        # Check required fields
        required_repr_fields = ["name", "ext", "stagingDir"]
        for field in required_repr_fields:
            if not representation.get(field):
                errors.append(
                    f"Representation {index} missing required field: {field}"
                )

        # Check staging directory exists
        staging_dir = representation.get("stagingDir")
        if staging_dir and not os.path.exists(staging_dir):
            errors.append(
                f"Representation {index} staging directory does not exist: {staging_dir}"
            )

        # Check files exist
        files = representation.get("files")
        if files:
            if isinstance(files, str):
                file_path = os.path.join(staging_dir or "", files)
                if not os.path.exists(file_path):
                    errors.append(
                        f"Representation {index} file does not exist: {file_path}"
                    )
            elif isinstance(files, list):
                for file_name in files:
                    file_path = os.path.join(staging_dir or "", file_name)
                    if not os.path.exists(file_path):
                        errors.append(
                            f"Representation {index} file does not exist: {file_path}"
                        )

        # Check for kitsureview tag
        tags = representation.get("tags", [])
        if "kitsureview" not in tags:
            errors.append(f"Representation {index} missing 'kitsureview' tag")

        return errors
