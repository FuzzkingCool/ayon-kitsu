"""Jump to Kitsu task action for Scene Inventory."""

import webbrowser
import os

import ayon_api

from ayon_core.pipeline import InventoryAction, get_current_project_name


def _kitsu_addon():
    from ayon_core.pipeline.context_tools import _get_addons_manager

    return _get_addons_manager().get("kitsu")


try:
    import gazu
except ImportError:
    gazu = None


# Get path to Kitsu logo
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_KITSU_ROOT = os.path.dirname(os.path.dirname(_CURRENT_DIR))
_KITSU_LOGO_PATH = os.path.join(_KITSU_ROOT, "vendor", "kitsu-logo.png")


class InventoryOpenInKitsu(InventoryAction):
    """Open Kitsu task discussion in browser for selected container."""

    label = "Jump to Kitsu Task"
    icon = _KITSU_LOGO_PATH
    color = "#e0e1e1"
    order = 100

    def is_compatible(self, container):
        """Check if action is compatible with container.

        Args:
            container (dict): Container data from host.ls()

        Returns:
            bool: True if container has representation that can be mapped to Kitsu
        """
        try:
            # Check if gazu is available
            if gazu is None:
                return False

            # Check if Kitsu addon is available
            kitsu_addon = _kitsu_addon()
            if not kitsu_addon:
                return False

            # Check if Kitsu credentials are available
            server = os.environ.get("KITSU_SERVER")
            login = os.environ.get("KITSU_LOGIN")
            password = os.environ.get("KITSU_PWD")
            if not all([server, login, password]):
                return False

            # Check if container has representation ID
            return bool(container.get("representation"))
        except Exception as e:
            # Log but don't break Scene Inventory
            self.log.debug(f"Kitsu action compatibility check failed: {e}")
            return False

    def process(self, containers):
        """Open Kitsu task for selected containers.

        Args:
            containers (list): List of container dictionaries

        Returns:
            bool: True if successful
        """
        try:
            kitsu_addon = _kitsu_addon()
            if not kitsu_addon:
                self.log.error("Kitsu addon not available")
                return False

            project_name = get_current_project_name()
            if not project_name:
                self.log.error("No project selected")
                return False

            # Get project entity with Kitsu ID
            project_entity = ayon_api.get_project(project_name)
            if not project_entity:
                self.log.error(f"Project {project_name} not found")
                return False

            kitsu_project_id = project_entity["data"].get("kitsuProjectId")
            if not kitsu_project_id:
                self.log.error(
                    f"Project {project_name} has no Kitsu project ID. "
                    "Please sync with Kitsu first."
                )
                return False

            # Login to Kitsu
            if not self._ensure_kitsu_logged_in():
                self.log.error(
                    "Failed to login to Kitsu. Please check your credentials."
                )
                return False

            # Process each container
            opened_count = 0
            for container in containers:
                if not self.is_compatible(container):
                    self.log.debug(f"Container not compatible: {container.get('objectName')}")
                    continue

                repre_id = container.get("representation")
                if not repre_id:
                    self.log.debug(f"No representation ID in container: {container.get('objectName')}")
                    continue

                # Get representation entity
                repre_entity = ayon_api.get_representation_by_id(
                    project_name, repre_id
                )
                if not repre_entity:
                    self.log.warning(f"Representation {repre_id} not found")
                    continue

                # Get version ID
                version_id = repre_entity.get("versionId")
                if not version_id:
                    self.log.warning(
                        f"Representation {repre_id} has no version ID"
                    )
                    continue

                # Jump to Kitsu task
                if self._jump_to_kitsu_for_version(
                    project_name, version_id, kitsu_project_id
                ):
                    opened_count += 1

            if opened_count > 0:
                self.log.info(f"Opened {opened_count} Kitsu task(s) in browser")
                return True

            self.log.error(
                "Could not open any Kitsu tasks. "
                "The selected containers may not have Kitsu IDs."
            )
            return False

        except Exception as e:
            self.log.error(f"Failed to open Kitsu task: {e}", exc_info=True)
            return False

    def _ensure_kitsu_logged_in(self):
        """Ensure we're logged into Kitsu using environment credentials."""
        server = os.environ.get("KITSU_SERVER")
        login = os.environ.get("KITSU_LOGIN")
        password = os.environ.get("KITSU_PWD")

        if not all([server, login, password]):
            return False

        try:
            # Check if already logged in
            if not gazu.client.get_host():
                # Set host and login
                gazu.set_host(server)
                gazu.log_in(login, password)
            return True
        except Exception as e:
            self.log.error(f"Failed to login to Kitsu: {e}")
            return False

    def _jump_to_kitsu_for_version(
        self, project_name, version_id, kitsu_project_id
    ):
        """Jump to Kitsu task for given version.

        Args:
            project_name (str): AYON project name
            version_id (str): AYON version ID
            kitsu_project_id (str): Kitsu project ID

        Returns:
            bool: True if successful
        """
        # Get version entity
        version_entity = ayon_api.get_version_by_id(project_name, version_id)
        if not version_entity:
            self.log.warning(f"Version {version_id} not found")
            return False

        # Get task ID from version
        task_id = version_entity.get("taskId")
        if not task_id:
            # Try to infer from product
            product_id = version_entity.get("productId")
            if product_id:
                product_entity = ayon_api.get_product_by_id(
                    project_name, product_id
                )
                if product_entity:
                    folder_id = product_entity.get("folderId")
                    if folder_id:
                        tasks = list(
                            ayon_api.get_tasks(
                                project_name, folder_ids=[folder_id]
                            )
                        )
                        if len(tasks) == 1:
                            task_id = tasks[0]["id"]

        if not task_id:
            self.log.warning(f"Cannot find task ID for version {version_id}")
            return False

        # Get task entity with Kitsu ID
        task_entity = ayon_api.get_task_by_id(project_name, task_id)
        if not task_entity:
            self.log.warning(f"Task {task_id} not found")
            return False

        # Get Kitsu task ID
        kitsu_task_id = task_entity.get("data", {}).get("kitsuId")
        if not kitsu_task_id:
            self.log.warning(f"Task {task_id} has no Kitsu ID")
            return False

        # Get Kitsu task to determine entity type
        try:
            kitsu_task = gazu.task.get_task(kitsu_task_id)
            if not kitsu_task:
                self.log.warning(f"Kitsu task {kitsu_task_id} not found")
                return False

            entity_id = kitsu_task.get("entity_id")
            if not entity_id:
                self.log.warning(
                    f"Kitsu task {kitsu_task_id} has no entity ID"
                )
                return False

            # Get entity to determine type (shot or asset)
            entity = gazu.entity.get_entity(entity_id)
            if not entity:
                self.log.warning(f"Kitsu entity {entity_id} not found")
                return False

            entity_type = entity.get("type")
            kitsu_type = (
                "shots" if entity_type in {"Shot", "Sequence"} else "assets"
            )

        except Exception as e:
            self.log.error(f"Failed to get Kitsu task info: {e}")
            return False

        # Build and open URL
        kitsu_addon = _kitsu_addon()
        kitsu_url = kitsu_addon.server_url.rstrip("/api")
        url = (
            f"{kitsu_url}/productions/{kitsu_project_id}/"
            f"{kitsu_type}/tasks/{kitsu_task_id}"
        )

        self.log.info(f"Opening Kitsu task: {url}")
        webbrowser.open(url, new=2)  # Try in new tab
        return True
