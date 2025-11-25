"""Open in Kitsu loader action for AYON Loader tool."""

import webbrowser
import os
import time

import ayon_api

from ayon_core.addon import AddonsManager

try:
    import gazu

    HAS_GAZU = True
except ImportError:
    gazu = None
    HAS_GAZU = False

# Get path to Kitsu logo
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_KITSU_ROOT = os.path.dirname(os.path.dirname(_CURRENT_DIR))
_KITSU_LOGO_PATH = os.path.join(_KITSU_ROOT, "vendor", "kitsu-logo.png")


def _get_base_class():
    """Get ProductLoaderPlugin without polluting module namespace.

    This function-based import prevents the discovery system from
    finding ProductLoaderPlugin in this module's namespace.
    """
    from ayon_core.pipeline import ProductLoaderPlugin

    return ProductLoaderPlugin


class OpenInKitsu(_get_base_class()):
    """Open Kitsu task discussion in browser for selected version.

    This is not a traditional "loader" - it doesn't load anything into the scene.
    Instead, it provides a context menu action to open the Kitsu task discussion
    in your browser.

    When run on a group, only opens one browser tab (prioritizes review items).
    """

    product_types = {"*"}  # Works with any product type
    representations = {"*"}  # Works with any representation
    extensions = {"*"}  # Works with any extension

    label = "Open in Kitsu"
    order = -100  # Negative order puts it at the top of the menu
    icon = "external-link-square"
    color = "#e0e1e1"

    # This makes it appear in the versions context menu
    show_in_versions_menu = True

    # Don't show in the loader itself (if that exists)
    enabled = True

    # Class variable to track recent opens and prevent duplicates
    # Format: {task_id: timestamp}
    _recent_opens = {}

    @classmethod
    def is_compatible_loader(cls, context):
        """Check if this action is available.

        Args:
            context (dict): Context with representation and product info

        Returns:
            bool: True if Kitsu is available and configured
        """
        if not HAS_GAZU:
            return False

        # Check if Kitsu addon is available
        kitsu_addon = AddonsManager().get("kitsu")
        if not kitsu_addon:
            return False

        # Check if we have Kitsu credentials
        server = os.environ.get("KITSU_SERVER")
        login = os.environ.get("KITSU_LOGIN")
        password = os.environ.get("KITSU_PWD")

        return all([server, login, password])

    def load(self, context, name=None, namespace=None, options=None):
        """Open Kitsu task in browser.

        This doesn't actually "load" anything - it just opens a browser URL.

        When called on a group, only opens once (prioritizes review items).

        Args:
            context (dict): Context with version, product, representation info
            name (str, optional): Unused
            namespace (str, optional): Unused
            options (dict, optional): Unused

        Returns:
            bool: True if successful
        """
        # Get version ID from context
        version_entity = context.get("version")
        if not version_entity:
            self.log.warning("No version in context")
            return False

        version_id = version_entity.get("id")
        if not version_id:
            self.log.warning("No version ID in context")
            return False

        # Get project name
        project_entity = context.get("project")
        if not project_entity:
            self.log.warning("No project in context")
            return False

        project_name = project_entity.get("name")
        if not project_name:
            self.log.warning("No project name in context")
            return False

        # Get product info to check type
        product_entity = context.get("product")
        product_type = None
        if product_entity:
            product_type = product_entity.get("productType")

        # Get Kitsu project ID
        kitsu_project_id = project_entity.get("data", {}).get("kitsuProjectId")
        if not kitsu_project_id:
            self.log.warning(f"Project {project_name} has no Kitsu project ID")
            return False

        # Login to Kitsu
        if not self._ensure_kitsu_logged_in():
            self.log.warning("Failed to login to Kitsu")
            return False

        # Check if this is a review product type (higher priority)
        is_review = product_type and "review" in product_type.lower()

        # Get task and check if we should open
        return self._open_kitsu_for_version(
            project_name, version_id, kitsu_project_id, is_review
        )

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

    def _open_kitsu_for_version(
        self, project_name, version_id, kitsu_project_id, is_review=False
    ):
        """Open Kitsu task for given version.

        Args:
            project_name (str): AYON project name
            version_id (str): AYON version ID
            kitsu_project_id (str): Kitsu project ID
            is_review (bool): Whether this is a review product (higher priority)

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

        # Check if we've recently opened this task (within 3 seconds)
        # This prevents opening multiple tabs when clicking on a group
        current_time = time.time()
        last_open_time = self._recent_opens.get(kitsu_task_id, 0)

        if current_time - last_open_time < 3.0:
            # Already opened recently
            if is_review:
                # This is a review item and we already opened something
                # Check if the previous open was also a review
                # If not, we should open this one instead
                self.log.debug(
                    f"Task {kitsu_task_id} opened recently, "
                    "but this is a review - opening anyway"
                )
                # Clear the previous entry so we open this review
                self._recent_opens.pop(kitsu_task_id, None)
            else:
                # Not a review and already opened recently - skip
                self.log.info(
                    f"Skipping Kitsu task {kitsu_task_id} "
                    "(already opened in this batch)"
                )
                return True  # Return True to not show as error

        # Clean up old entries (older than 10 seconds)
        self._recent_opens = {
            tid: t
            for tid, t in self._recent_opens.items()
            if current_time - t < 10.0
        }

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
        kitsu_addon = AddonsManager().get("kitsu")
        kitsu_url = kitsu_addon.server_url.rstrip("/api")
        url = (
            f"{kitsu_url}/productions/{kitsu_project_id}/"
            f"{kitsu_type}/tasks/{kitsu_task_id}"
        )

        # Record this open
        self._recent_opens[kitsu_task_id] = current_time

        self.log.info(f"Opening Kitsu task: {url}")
        webbrowser.open(url, new=2)  # Try in new tab
        return True
