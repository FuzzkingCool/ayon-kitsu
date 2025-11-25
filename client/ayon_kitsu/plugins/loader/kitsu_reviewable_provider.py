"""Kitsu reviewable provider for AYON loader tool.

Fetches preview/reviewable videos from Kitsu when available.

This provider works in conjunction with the Kitsu publish plugins:
- CollectKitsuEntities: Sets kitsuId on task entities during sync
- IntegrateKitsuReview: Uploads previews to Kitsu as task comments

The mapping works as follows:
1. AYON Version → Product → Task
2. Task entity has kitsuId in data (set during sync)
3. Use kitsuId to fetch Kitsu task
4. Get preview files from Kitsu task
5. Download and cache for playback
"""

import os
import tempfile
from typing import Optional

import gazu

from ayon_core.tools.loader.providers import ReviewableProvider


class KitsuReviewableProvider(ReviewableProvider):
    """Provider for Kitsu preview files.

    Retrieves reviewable videos that have been uploaded to Kitsu
    as task previews/reviews. Downloads them to a local cache
    for playback.
    """

    priority = 20  # High priority but after activities
    identifier = "kitsu"
    label = "Kitsu"

    def __init__(self):
        super().__init__()
        self._cache_dir = None

    def _get_cache_dir(self):
        """Get or create cache directory for Kitsu previews."""
        if self._cache_dir is None:
            cache_base = tempfile.gettempdir()
            self._cache_dir = os.path.join(cache_base, "ayon_kitsu_previews")
            os.makedirs(self._cache_dir, exist_ok=True)
        return self._cache_dir

    def _ensure_kitsu_logged_in(self):
        """Ensure we're logged into Kitsu using environment credentials."""
        server = os.environ.get("KITSU_SERVER")
        login = os.environ.get("KITSU_LOGIN")
        password = os.environ.get("KITSU_PWD")

        if not all([server, login, password]):
            return False

        try:
            # Check if already logged in
            if gazu.client.get_host():
                return True

            # Set host and login
            gazu.set_host(server)
            gazu.log_in(login, password)
            return True
        except Exception:
            return False

    def _get_kitsu_task_for_version(
        self, project_name: str, version_id: str, controller
    ) -> Optional[dict]:
        """Get Kitsu task entity for AYON version.

        Maps AYON version → product → task entity, then gets kitsuId
        from task entity data to fetch the Kitsu task.

        Args:
            project_name (str): AYON project name
            version_id (str): AYON version ID
            controller: Loader controller

        Returns:
            Optional[dict]: Kitsu task entity or None
        """
        try:
            import ayon_api

            # Get AYON version entity
            version_entity = ayon_api.get_version_by_id(
                project_name, version_id
            )
            if not version_entity:
                controller.log.debug(f"Version {version_id} not found")
                return None

            # Get product and task info from version
            product_id = version_entity.get("productId")
            task_id = version_entity.get("taskId")

            if not product_id:
                controller.log.debug(f"No productId in version {version_id}")
                return None

            # Get product to find folder and task
            product_entity = ayon_api.get_product_by_id(
                project_name, product_id
            )
            if not product_entity:
                controller.log.debug(f"Product {product_id} not found")
                return None

            folder_id = product_entity.get("folderId")
            if not folder_id:
                controller.log.debug(f"No folderId in product {product_id}")
                return None

            # If no taskId from version, try to infer from product name
            # (e.g., "renderCompMain" → task "Comp")
            if not task_id:
                # Try to get all tasks for the folder
                tasks = list(
                    ayon_api.get_tasks(project_name, folder_ids=[folder_id])
                )

                # If only one task, use it
                if len(tasks) == 1:
                    task_id = tasks[0]["id"]
                    controller.log.debug(
                        f"Using single task {task_id} from folder"
                    )
                else:
                    controller.log.debug(
                        f"No taskId in version and multiple tasks in folder"
                    )
                    return None

            # Get task entity to find kitsuId
            task_entity = ayon_api.get_task_by_id(project_name, task_id)
            if not task_entity:
                controller.log.debug(f"Task {task_id} not found")
                return None

            # Get Kitsu task ID from AYON task data
            kitsu_task_id = task_entity.get("data", {}).get("kitsuId")

            if not kitsu_task_id:
                controller.log.debug(
                    f"No kitsuId in task {task_id} data for {folder_id}"
                )
                return None

            # Get Kitsu task from Kitsu API
            controller.log.debug(f"Fetching Kitsu task {kitsu_task_id}")
            task = gazu.task.get_task(kitsu_task_id)

            if not task:
                controller.log.warning(
                    f"Kitsu task {kitsu_task_id} not found in Kitsu"
                )
                return None

            return task

        except Exception as e:
            controller.log.debug(
                f"Failed to get Kitsu task for version {version_id}: {e}",
                exc_info=True,
            )
            return None

    def _download_preview(
        self, preview_file: dict, controller
    ) -> Optional[str]:
        """Download Kitsu preview file to local cache.

        Args:
            preview_file (dict): Kitsu preview file entity
            controller: Loader controller

        Returns:
            Optional[str]: Path to downloaded file or None
        """
        try:
            preview_id = preview_file.get("id")
            if not preview_id:
                return None

            # Create cache filename
            file_ext = preview_file.get("extension", "mp4")
            cache_file = os.path.join(
                self._get_cache_dir(), f"kitsu_preview_{preview_id}.{file_ext}"
            )

            # Check if already cached
            if os.path.exists(cache_file):
                controller.log.debug(
                    f"Using cached Kitsu preview: {cache_file}"
                )
                return cache_file

            # Download preview
            controller.log.debug(
                f"Downloading Kitsu preview {preview_id} to {cache_file}"
            )

            preview_url = gazu.files.build_preview_file_path(preview_file)

            # Download file
            import requests

            response = requests.get(preview_url, stream=True)
            response.raise_for_status()

            with open(cache_file, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            return cache_file

        except Exception as e:
            controller.log.warning(f"Failed to download Kitsu preview: {e}")
            return None

    def get_reviewable_path(
        self, project_name: str, version_ids: set, controller
    ) -> Optional[str]:
        """Get reviewable video from Kitsu.

        Args:
            project_name (str): AYON project name
            version_ids (set[str]): AYON version IDs
            controller: Loader controller

        Returns:
            Optional[str]: Path to downloaded reviewable or None
        """
        if not self._ensure_kitsu_logged_in():
            controller.log.debug(
                "Not logged into Kitsu, skipping Kitsu reviewable provider"
            )
            return None

        try:
            # Try each version
            for version_id in version_ids:
                # Get corresponding Kitsu task
                kitsu_task = self._get_kitsu_task_for_version(
                    project_name, version_id, controller
                )
                if not kitsu_task:
                    continue

                # Get preview files for task
                task_id = kitsu_task["id"]
                preview_files = gazu.task.all_preview_files_for_task(task_id)

                if not preview_files:
                    controller.log.debug(
                        f"No preview files found for Kitsu task {task_id}"
                    )
                    continue

                # Get the latest preview (they should be sorted by date)
                # Filter for video files only
                video_previews = [
                    p
                    for p in preview_files
                    if p.get("extension", "").lower()
                    in ["mp4", "mov", "avi", "mkv", "webm"]
                ]

                if not video_previews:
                    controller.log.debug(
                        f"No video previews found for Kitsu task {task_id}"
                    )
                    continue

                # Use the latest video preview
                latest_preview = video_previews[-1]

                # Download and return path
                preview_path = self._download_preview(
                    latest_preview, controller
                )
                if preview_path:
                    return preview_path

        except Exception as e:
            controller.log.warning(
                f"KitsuReviewableProvider failed: {e}", exc_info=True
            )

        return None

    def is_available(self, project_name: str, controller) -> bool:
        """Check if Kitsu integration is enabled and configured.

        Args:
            project_name (str): AYON project name
            controller: Loader controller

        Returns:
            bool: True if provider should be used
        """
        # Check if Kitsu credentials are set
        server = os.environ.get("KITSU_SERVER")
        login = os.environ.get("KITSU_LOGIN")
        password = os.environ.get("KITSU_PWD")

        if not all([server, login, password]):
            return False

        # Check if enabled in project settings
        try:
            from ayon_core.settings import get_project_settings

            settings = get_project_settings(project_name)
            kitsu_settings = settings.get("kitsu", {})

            # Check if Kitsu is enabled for this project
            if not kitsu_settings.get("enabled", False):
                return False

            # Check if reviewable provider is enabled
            loader_settings = (
                settings.get("core", {})
                .get("tools", {})
                .get("loader", {})
                .get("reviewable_providers", {})
            )

            return loader_settings.get("use_kitsu", False)

        except Exception:
            return False
