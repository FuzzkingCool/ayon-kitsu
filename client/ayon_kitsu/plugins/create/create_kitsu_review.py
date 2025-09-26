# -*- coding: utf-8 -*-
"""Kitsu Review Creator for Tray Publisher.

This creator allows users to create review instances that submit review representations
to Kitsu. It follows the traypublisher creator pattern and integrates with existing
Kitsu publishing workflow.
"""

import traceback
from pathlib import Path

from ayon_core.lib.attribute_definitions import (
    BoolDef,
    FileDef,
    UILabelDef,
    UISeparatorDef,
)
from ayon_core.pipeline import CreatedInstance, CreatorError
from ayon_core.pipeline.create import PRE_CREATE_THUMBNAIL_KEY

# Import Qt for widget access
from qtpy import QtCore, QtWidgets

# Import the traypublisher creator base class
try:
    from ayon_traypublisher.api.plugin import TrayPublishCreator
except ImportError:
    # Fallback if traypublisher is not available
    from ayon_core.pipeline.create import Creator as TrayPublishCreator


class KitsuReviewCreator(TrayPublishCreator):
    """Create Review instances for Kitsu submission.
    This creator integrates with traypublisher to allow submitting reviews
    directly to Kitsu. It creates instances that trigger Kitsu-specific
    publish plugins.
    """

    identifier = "io.ayon.creators.kitsu.review"
    label = "Kitsu Review"
    product_type = "review"
    description = "Submit review representations to Kitsu"

    # Supported extensions
    extensions = [
        ".mov",
        ".mp4",
        ".avi",
        ".mkv",
        ".m4v",
        ".jpg",
        ".jpeg",
        ".png",
        ".tga",
        ".exr",
    ]

    # Default variants
    default_variants = ["Main", "Client", "Internal"]

    # Enable thumbnail/screenshot capture
    create_allow_thumbnail = True

    # Store multiple screenshots
    _captured_screenshots = []  # Store multiple screenshots
    _last_thumbnail_path = None  # Track last known thumbnail
    _polling_timer = None  # Qt timer for polling thumbnail changes

    def _start_thumbnail_polling(self):
        """Start polling for thumbnail changes using a Qt timer."""
        # self.log.debug("KitsuReview: _start_thumbnail_polling called")

        if self._polling_timer is not None:
            # self.log.debug("KitsuReview: Polling timer already started")
            return  # Already started

        # Initial check
        self._check_thumbnail_changes()

        try:
            # Create a timer that checks for thumbnail changes every 2 seconds
            self._polling_timer = QtCore.QTimer()
            self._polling_timer.timeout.connect(self._check_thumbnail_changes)
            self._polling_timer.start(2000)  # Check every 2 seconds
            # log.info(
            #     "KitsuReview: Started thumbnail polling timer (2s interval)"
            # )
        except Exception as exc:
            self.log.error(
                f"KitsuReview: Failed to start thumbnail polling: {exc}"
            )

            self.log.debug(
                f"KitsuReview: Polling error traceback: {traceback.format_exc()}"
            )

    def _check_thumbnail_changes(self):
        """Check if the thumbnail has changed and update FileDef if needed."""
        try:
            # Get the current thumbnail path from the create context
            current_thumbnail = None

            # The correct way: Access the CreateWidget's _last_thumbnail_path
            # This is where AYON stores the captured thumbnail before creation
            try:
                # Find the CreateWidget in the Qt application
                if QtWidgets:
                    app = QtWidgets.QApplication.instance()
                    if app:
                        for widget in app.allWidgets():
                            # Look for CreateWidget
                            if widget.__class__.__name__ == "CreateWidget":
                                if hasattr(widget, "_last_thumbnail_path"):
                                    current_thumbnail = (
                                        widget._last_thumbnail_path
                                    )
                                    break

            except Exception as exc:
                self.log.debug(
                    f"KitsuReview: Error accessing CreateWidget thumbnail: {exc}"
                )

            # Only log when something actually changes
            if (
                current_thumbnail
                and current_thumbnail != self._last_thumbnail_path
            ):
                # self.log.info(
                #     f"KitsuReview: New thumbnail detected: {current_thumbnail}"
                # )
                self._last_thumbnail_path = current_thumbnail
                # Update the FileDef with the new thumbnail
                self._update_filedef_with_thumbnail(current_thumbnail)
            elif current_thumbnail != self._last_thumbnail_path:
                # Thumbnail was cleared or set to None
                self._last_thumbnail_path = current_thumbnail

        except Exception as exc:
            self.log.error(
                f"KitsuReview: Error checking thumbnail changes: {exc}"
            )
            import traceback

            self.log.debug(
                f"KitsuReview: Thumbnail check error traceback: {traceback.format_exc()}"
            )

    def register_callbacks(self):
        """Register callbacks for value changes and screenshot capture."""
        # self.log.debug("KitsuReview: register_callbacks called")

        # Start thumbnail polling - much simpler than event registration
        self._start_thumbnail_polling()

        try:
            if hasattr(self.create_context, "add_value_changed_callback"):
                self.create_context.add_value_changed_callback(
                    self.on_values_changed
                )
                # self.log.info(
                #     "KitsuReview: Successfully registered value changed callback"
                # )
            else:
                self.log.warning(
                    "KitsuReview: No add_value_changed_callback method found"
                )
        except Exception as exc:
            self.log.error(
                f"KitsuReview: Failed to register value changed callback: {exc}"
            )

        # Apply widget sizing after delay (create_render.py pattern)
        QtCore.QTimer.singleShot(500, self.apply_all_widget_sizing)

    def on_values_changed(self, event):
        """Handle UI value changes (placeholder for future enhancements)."""
        self.log.info(f"KitsuReview: Values changed event received: {event}")
        self._check_thumbnail_changes()

    def apply_widget_sizing(
        self, widget_key, min_width=None, max_width=None, fixed_width=None
    ):
        """Apply sizing constraints to a specific widget by its attribute key.

        Args:
            widget_key (str): The attribute definition key
            min_width (int, optional): Minimum width in pixels
            max_width (int, optional): Maximum width in pixels
            fixed_width (int, optional): Fixed width in pixels (overrides min/max)
        """
        try:
            from ayon_core.tools.attribute_defs.widgets import (
                AttributeDefinitionsWidget,
            )

            app = QtWidgets.QApplication.instance()
            if not app:
                return False

            # Find all AttributeDefinitionsWidget instances
            for widget in app.allWidgets():
                if not isinstance(widget, AttributeDefinitionsWidget):
                    continue

                # Access the internal widgets by their attribute definition IDs
                for attr_def_id, attr_widget in widget._widgets_by_id.items():
                    attr_def = attr_widget.attr_def

                    if attr_def.key == widget_key:
                        # Find the actual input widget
                        input_widget = getattr(
                            attr_widget, "_input_widget", None
                        )
                        if input_widget:
                            # Apply sizing constraints
                            if fixed_width is not None:
                                input_widget.setFixedWidth(fixed_width)
                            else:
                                if min_width is not None:
                                    input_widget.setMinimumWidth(min_width)
                                if max_width is not None:
                                    input_widget.setMaximumWidth(max_width)

                            self.log.debug(
                                f"Applied sizing to {widget_key}: min={min_width}, max={max_width}, fixed={fixed_width}"
                            )
                            return True

            return False

        except Exception as e:
            self.log.debug(
                f"Failed to apply widget sizing for {widget_key}: {e}"
            )
            return False

    def apply_all_widget_sizing(self):
        """Apply sizing to FileDef widget to prevent UI issues."""
        # Apply sizing to the review_files FileDef widget
        self.apply_widget_sizing("review_files", min_width=300)

    def _is_current_creator(self):
        """Check if this creator is currently selected in the UI."""
        # Simplified version - just return True for now to avoid loading issues
        # Complex Qt widget detection can cause creator loading failures
        return True

    def _update_filedef_with_thumbnail(self, thumbnail_path):
        """Update the review_files attribute with the captured thumbnail.

        Args:
            thumbnail_path (str): Path to the captured thumbnail
        """
        try:
            self.log.info(
                f"KitsuReview: Adding thumbnail to review_files: {thumbnail_path}"
            )

            # The canonical AYON way: work with pre-create attribute definitions
            # Get current attribute definitions and find review_files
            attr_defs = self.get_pre_create_attr_defs()
            review_files_def = None
            for attr_def in attr_defs:
                if hasattr(attr_def, "key") and attr_def.key == "review_files":
                    review_files_def = attr_def
                    break

            if not review_files_def:
                self.log.warning(
                    "KitsuReview: Could not find review_files attribute definition"
                )
                return

            # First, copy and rename the screenshot to a numbered format
            thumbnail_path = Path(thumbnail_path)

            # Generate screenshot counter
            if not hasattr(self, "_screenshot_counter"):
                self._screenshot_counter = 0
            self._screenshot_counter += 1

            # Create a new filename with numbered format
            new_filename = f"Screenshot_{self._screenshot_counter:02d}.png"
            new_path = thumbnail_path.parent / new_filename

            # Copy the original file to the new name
            import shutil

            shutil.copy2(str(thumbnail_path), str(new_path))

            # Use the renamed file for the rest of the process
            thumbnail_path = new_path
            thumbnail_path_str = str(thumbnail_path)

            # Create FileDef value structure with the thumbnail
            from ayon_core.lib.attribute_definitions import FileDefItem

            # Create a FileDefItem and convert to proper dict format
            file_item = FileDefItem(
                str(thumbnail_path.parent), [thumbnail_path.name]
            )
            filedef_value = file_item.to_dict()
            self.log.debug(
                f"KitsuReview: Created FileDef value: {filedef_value}"
            )

            # Store this as the default value for new instances
            review_files_def.default = filedef_value

            # CRITICAL: Add to pending thumbnails list for FileDef default
            if not hasattr(self, "_pending_thumbnails"):
                self._pending_thumbnails = []

            # Check if this thumbnail is already in the list (avoid duplicates)
            thumbnail_already_added = False
            for existing_thumb in self._pending_thumbnails:
                if existing_thumb.get("directory") == filedef_value.get(
                    "directory"
                ) and existing_thumb.get("filenames") == filedef_value.get(
                    "filenames"
                ):
                    thumbnail_already_added = True
                    break

            if not thumbnail_already_added:
                self._pending_thumbnails.append(filedef_value)
                # self.log.debug(
                #     f"KitsuReview: Added thumbnail to pending list: {filedef_value}"
                # )
                # self.log.debug(
                #     f"KitsuReview: Total pending thumbnails: {len(self._pending_thumbnails)}"
                # )
            else:
                self.log.debug(
                    "KitsuReview: Thumbnail already in pending list, skipping duplicate"
                )

            # Add to the existing captured screenshots list for create() method
            if not hasattr(self, "_captured_screenshots"):
                self._captured_screenshots = []
            if thumbnail_path_str not in self._captured_screenshots:
                self._captured_screenshots.append(thumbnail_path_str)
                self.log.debug(
                    f"KitsuReview: Added to captured screenshots: {thumbnail_path_str}"
                )

            # Update the FileDef widget to show the screenshot in the UI
            try:
                app = QtWidgets.QApplication.instance()
                if app:
                    # Find the FileDef widget by looking for review_files attribute
                    for widget in app.allWidgets():
                        # Look for the attribute widget that has our key
                        if (
                            hasattr(widget, "attr_def")
                            and hasattr(widget.attr_def, "key")
                            and widget.attr_def.key == "review_files"
                        ):
                            # Found our widget! Get current value and append to it
                            if hasattr(widget, "set_value") and hasattr(
                                widget, "current_value"
                            ):
                                current_files = widget.current_value() or []

                                # Ensure current_files is a list
                                if not isinstance(current_files, list):
                                    current_files = []

                                # Add the new screenshot if it's not already there
                                if thumbnail_path_str not in current_files:
                                    current_files.append(thumbnail_path_str)

                                    # Set the updated list (this will accumulate)
                                    widget.set_value(current_files, False)
                                    self.log.debug(
                                        f"KitsuReview: Added screenshot to FileDef UI: {thumbnail_path.name}"
                                    )

                                return  # Success

            except Exception as widget_exc:
                self.log.error(
                    f"KitsuReview: Widget update error: {widget_exc}"
                )

            # Trigger UI refresh to rebuild FileDef with the thumbnail
            self.create_context.create_plugin_pre_create_attr_defs_changed(
                self.identifier
            )

            self.log.info(
                f"KitsuReview: Successfully added thumbnail to review_files: {thumbnail_path.name}"
            )

        except Exception as exc:
            self.log.error(
                f"KitsuReview: Error updating review_files with thumbnail: {exc}"
            )
            import traceback

            self.log.debug(
                f"KitsuReview: Update error traceback: {traceback.format_exc()}"
            )

    def get_detail_description(self):
        return """# Submit Review to Kitsu
This creator allows you to submit review files directly to Kitsu
without creating AYON representations. The review files are uploaded
to Kitsu as previews for the associated task.

Features:
- Direct Kitsu upload
- Screenshot capture or file selection
- Task status updates
- Support for video and image files
- Comments handled by standard publish workflow
"""

    def get_icon(self):
        """Return the icon for this creator."""
        return "fa.eye"

    def apply_settings(self, project_settings):
        """Apply project settings (from base class)."""
        # Call parent implementation if it exists
        if hasattr(super(), "apply_settings"):
            super().apply_settings(project_settings)

    def create(self, product_name, instance_data, pre_create_data):
        """Create a new Kitsu review instance.

        Args:
            product_name (str): Name of the product to create
            instance_data (dict): Base instance data
            pre_create_data (dict): Data from pre-create attributes
        """
        self.log.info(f"KitsuReview: Creating instance '{product_name}'")

        # Get task name from instance data (TrayPublisher pattern)
        task_name = instance_data.get("task")
        if not task_name or task_name == "None":
            raise CreatorError(
                "No task selected. Please select a task in TrayPublisher before creating a Kitsu review."
            )

        # Get folder entity to fetch task entity
        folder_path = instance_data["folderPath"]
        folder_entity = self.create_context.get_folder_entity(folder_path)

        # Get task entity using ayon_api (following TrayPublisher pattern)
        import ayon_api

        task_entity = ayon_api.get_task_by_name(
            self.project_name, folder_entity["id"], task_name
        )

        if not task_entity:
            raise CreatorError(
                f"Task '{task_name}' not found in folder '{folder_path}'"
            )

        self.log.info(f"KitsuReview: Creating review for task: {task_name} (ID: {task_entity['id']})")

        # self.log.debug(
        #     f"KitsuReview: pre_create_data keys: {list(pre_create_data.keys())}"
        # )

        # Get review files from pre-create data (following traypublisher pattern)
        review_files_data = pre_create_data.get("review_files")
        screenshot_path = pre_create_data.pop(PRE_CREATE_THUMBNAIL_KEY, None)

        # self.log.debug(f"KitsuReview: review_files_data: {review_files_data}")
        # self.log.debug(
        #     f"KitsuReview: screenshot_path from PRE_CREATE_THUMBNAIL_KEY: {screenshot_path}"
        # )

        # Collect all files (from FileDef + screenshot)
        all_files = []
        main_directory = None

        # Add files from FileDef
        if review_files_data:
            # Handle both list of file paths and FileDef dictionary format
            if isinstance(review_files_data, list):
                # List can contain either strings or dictionaries (FileDef format)
                for item in review_files_data:
                    if isinstance(item, str):
                        # Direct file path string
                        file_path = Path(item)
                        if file_path.exists():
                            all_files.append(file_path)
                            if not main_directory:
                                main_directory = str(file_path.parent)
                    elif isinstance(item, dict):
                        # FileDef dictionary format
                        files = item.get("filenames", [])
                        directory = item.get("directory")
                        if files and directory:
                            if not main_directory:
                                main_directory = directory
                            for filename in files:
                                file_path = Path(directory) / filename
                                if file_path.exists():
                                    all_files.append(file_path)
            elif isinstance(review_files_data, dict):
                # Single FileDef dictionary format
                files = review_files_data.get("filenames", [])
                directory = review_files_data.get("directory")
                if files and directory:
                    main_directory = directory
                    for filename in files:
                        file_path = Path(directory) / filename
                        if file_path.exists():
                            all_files.append(file_path)

        # Add screenshot if captured (from thumbnail system)
        if screenshot_path:
            screenshot_file = Path(screenshot_path)
            if screenshot_file.exists():
                all_files.append(screenshot_file)
                # If no other files, use screenshot directory
                if not main_directory:
                    main_directory = str(screenshot_file.parent)

                # Screenshot is already included via polling system

        # Add any additional screenshots captured via our custom method
        for additional_screenshot in self._captured_screenshots:
            screenshot_file = Path(additional_screenshot)
            if screenshot_file.exists():
                all_files.append(screenshot_file)
                # If no other files, use screenshot directory
                if not main_directory:
                    main_directory = str(screenshot_file.parent)

        # Add screenshots from pending thumbnails (FileDef format)
        pending_thumbnails = getattr(self, "_pending_thumbnails", [])
        for thumbnail_data in pending_thumbnails:
            if isinstance(thumbnail_data, dict):
                directory = thumbnail_data.get("directory", "")
                filenames = thumbnail_data.get("filenames", [])
                for filename in filenames:
                    file_path = Path(directory) / filename
                    if file_path.exists():
                        all_files.append(file_path)
                        if not main_directory:
                            main_directory = str(file_path.parent)

        # Must have at least one file

        if not all_files:
            raise CreatorError(
                "No files specified. Please drag and drop files or capture a screenshot."
            )

        # Use the first file as the main review file for Kitsu upload
        main_review_file = all_files[0]

        # Set up instance data following traypublisher pattern
        instance_data["creator_attributes"] = {
            "path": main_review_file.as_posix(),
            "set_review_status": pre_create_data.get(
                "set_review_status", False
            ),
        }

        # Store review file path for Kitsu-only integration
        instance_data["reviewFile"] = main_review_file.as_posix()
        instance_data["kitsuOnlyReview"] = (
            True  # Flag for Kitsu-only processing
        )

        # Ensure task name is properly set
        if not instance_data.get("task"):
            instance_data["task"] = task_name

        # Store task entity for publish plugins
        instance_data["taskEntity"] = task_entity
        instance_data["folderEntity"] = folder_entity

        # Create representations for all files (including screenshots)
        representations = []
        for i, file_path in enumerate(all_files):
            file_ext = file_path.suffix.lower().lstrip(".")

            # Determine representation name based on file extension
            if file_ext in ["mov", "mp4", "avi", "mkv", "m4v", "mpg"]:
                repr_name = file_ext
            elif file_ext in ["jpg", "jpeg"]:
                repr_name = "jpg"
            elif file_ext in ["tif", "tiff"]:
                repr_name = "tif"
            elif file_ext in ["png", "exr", "pdf"]:
                repr_name = file_ext
            else:
                repr_name = file_ext

            # Create representation
            representation = {
                "name": repr_name,
                "ext": file_ext,
                "files": file_path.name,
                "stagingDir": str(file_path.parent),
                "tags": ["kitsureview"],
            }

            # Tag the first file as the main review
            if i == 0:
                representation["tags"].append("review")
            else:
                representation["tags"].append("additional")

            representations.append(representation)

        # Store representations
        instance_data["representations"] = representations

        # Add Kitsu-specific families to trigger Kitsu processing
        families = instance_data.get("families", [])

        # Add families needed for Kitsu filtering, avoiding duplicates
        required_families = ["review", "kitsu"]
        for family in required_families:
            if family not in families:
                families.append(family)

        instance_data["families"] = families

        # Create new instance
        new_instance = CreatedInstance(
            self.product_type, product_name, instance_data, self
        )
        self._store_new_instance(new_instance)

    def get_pre_create_attr_defs(self):
        """Return attribute definitions for pre-create dialog."""
        # log.debug("KitsuReview: get_pre_create_attr_defs called")

        # Start thumbnail polling when UI is being built
        # This is much simpler and more reliable than event registration
        self._start_thumbnail_polling()

        # Check if we have pending thumbnails to include
        pending_thumbnails = getattr(self, "_pending_thumbnails", [])

        # For FileDef with single_item=False, use the pending thumbnails (FileDef format)
        default_files = pending_thumbnails if pending_thumbnails else None

        # Debug: Check if the files actually exist
        for thumbnail_data in pending_thumbnails:
            if isinstance(thumbnail_data, dict):
                directory = thumbnail_data.get("directory", "")
                filenames = thumbnail_data.get("filenames", [])
                for filename in filenames:
                    file_path = Path(directory) / filename
                    import os

                    exists = os.path.exists(file_path)
                    self.log.debug(
                        f"KitsuReview: File exists check: {file_path} -> {exists}"
                    )
                    if not exists:
                        self.log.warning(
                            f"KitsuReview: File does not exist: {file_path}"
                        )

        # Create the FileDef with proper logging
        file_def = FileDef(
            "review_files",
            folders=False,
            extensions=self.extensions,
            allow_sequences=True,
            single_item=False,  # Allow multiple files
            label="Review Files",
            default=default_files,
        )

        # self.log.debug(
        #     f"KitsuReview: Created FileDef with default: {file_def.default}"
        # )
        # self.log.debug(f"KitsuReview: FileDef single_item: {file_def.single_item}")

        attr_defs = [
            file_def,
        ]

        attr_defs.append(
            UILabelDef(
                "Tip: Use the camera button (📷) in the top-right to capture screenshots, or drag files below"
            )
        )

        attr_defs.extend(
            [
                UISeparatorDef(),
                UILabelDef("Options"),
                BoolDef(
                    "set_review_status",
                    label="Set Review Status",
                    tooltip="Update task status when submitting review",
                    default=False,
                ),
            ]
        )

        return attr_defs

    def get_instance_attr_defs(self):
        """Return attribute definitions for instance editing."""
        return [
            BoolDef("add_review_family", default=True, label="Review"),
        ]
