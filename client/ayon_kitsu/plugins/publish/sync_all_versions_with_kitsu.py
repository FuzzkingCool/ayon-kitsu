"""Universal version synchronization with Kitsu for all product types and hosts."""

import collections

import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishContextPlugin


class SyncAllVersionsWithKitsu(KitsuPublishContextPlugin):
    """Synchronize version numbers for ALL instances using Ayon and Kitsu data.

    This plugin processes ALL instances (workfiles, templates, renders, reviews, etc.)
    across ALL hosts (Harmony, Photoshop, Maya, etc.) to ensure they use consistent
    versioning by comparing:
    - Latest version from Ayon database (productType-specific)
    - Latest revision from Kitsu (collected by CollectKitsuLatestReviewVersion)

    The highest value + 1 becomes the target version for ALL instances publishing
    to the same task, preventing clobbering of manually uploaded Kitsu versions.
    """

    label = "Sync All Versions with Kitsu"
    order = (
        pyblish.api.CollectorOrder + 0.48
    )  # Run after CollectKitsuLatestReviewVersion (0.479) but before other sync plugins
    # No hosts restriction - this works for ALL hosts

    def process(self, context):
        """Process all instances and sync their versions with Kitsu data."""
        project_name = context.data.get("projectName")
        if not project_name:
            self.log.warning(
                "No project name found, skipping Kitsu version sync"
            )
            return

        # Get all instances that need version synchronization
        instances_to_sync = self._get_instances_to_sync(context)

        if not instances_to_sync:
            self.log.debug(
                "No instances found that need Kitsu version synchronization"
            )
            return

        self.log.debug(
            "SyncAllVersionsWithKitsu: %s instance(s) to synchronize",
            len(instances_to_sync),
        )

        # Group instances by task for coordinated versioning
        task_groups = self._group_instances_by_task(instances_to_sync)

        # Sync versions for each task group
        for task_name, task_instances in task_groups.items():
            self._sync_versions_for_task_group(
                task_name, task_instances, project_name
            )

    def _get_instances_to_sync(self, context):
        """Get all instances that need version synchronization.

        Returns:
            list: List of instances that should be synchronized
        """
        instances_to_sync = []

        for instance in context:
            # Skip instances already processed by other sync plugins
            if instance.data.get("versionSynced"):
                self.log.debug(
                    f"Skipping already synced instance: {instance.data.get('productName')}"
                )
                continue

            # Skip instances that don't participate in versioning
            if not self._should_sync_instance(instance):
                continue

            instances_to_sync.append(instance)

        return instances_to_sync

    def _should_sync_instance(self, instance):
        """Check if an instance should participate in version synchronization.

        Returns:
            bool: True if instance should be synchronized
        """
        product_type = instance.data.get("productType", "")

        # Skip instances without proper context data
        if not instance.data.get("folderPath") or not instance.data.get(
            "task"
        ):
            self.log.debug(
                f"Skipping instance without proper context: {instance.data.get('productName')}"
            )
            return False

        # Include all major product types that should coordinate versions across all hosts
        syncable_types = {
            # Core product types
            "workfile",
            "render",
            "renderlayer",
            "review",
            # Harmony-specific
            "harmony.template",
            "harmony.layeredtemplate",
            "harmony.palette",
            # Photoshop-specific
            "image",
            "psd",
            # Maya-specific
            "model",
            "rig",
            "animation",
            "camera",
            "mayaScene",
            # After Effects-specific
            "aftereffects.scene",
            # Houdini-specific
            "houdini.scene",
            "vdb",
            "abc",
            # Nuke-specific
            "nuke.script",
            "nukescript",
            # Blender-specific
            "blender.scene",
            # Unreal-specific
            "unreal.scene",
            # Generic types
            "plate",
            "movie",
            "audio",
            "texture",
            "look",
            "layout",
        }

        if product_type in syncable_types:
            return True

        # Also sync any instance with families that include syncable types
        families = instance.data.get("families", [])
        if any(family in syncable_types for family in families):
            return True

        self.log.debug(f"Skipping non-syncable product type: {product_type}")
        return False

    def _group_instances_by_task(self, instances):
        """Group instances by task for coordinated versioning.

        Returns:
            dict: Dictionary mapping task names to lists of instances
        """
        task_groups = {}

        for instance in instances:
            task_name = instance.data.get("task")
            folder_path = instance.data.get("folderPath")

            # Create a unique key for each task context
            task_key = f"{folder_path}::{task_name}"

            if task_key not in task_groups:
                task_groups[task_key] = []
            task_groups[task_key].append(instance)

        return task_groups

    def _sync_versions_for_task_group(self, task_key, instances, project_name):
        """Sync versions for all instances in a task group.

        Args:
            task_key (str): Task identifier (folderPath::taskName)
            instances (list): List of instances for this task
            project_name (str): Project name
        """
        self.log.debug(
            "SyncAllVersionsWithKitsu: task %s (%s instances)",
            task_key,
            len(instances),
        )

        # Get the highest Kitsu version across all instances in this task
        max_kitsu_version = 0
        kitsu_instances = []

        kitsu_by_product = [
            (
                inst.data.get("productName", "unknown"),
                inst.data.get("kitsuLatestVersion", 0),
            )
            for inst in instances
        ]
        self.log.debug(
            "Kitsu latestVersion by product (task %s): %s",
            task_key,
            kitsu_by_product,
        )

        for instance in instances:
            kitsu_latest = instance.data.get("kitsuLatestVersion", 0)
            product_name = instance.data.get("productName", "unknown")

            if kitsu_latest and kitsu_latest > max_kitsu_version:
                max_kitsu_version = kitsu_latest

            if kitsu_latest:
                kitsu_instances.append(product_name)

        if max_kitsu_version > 0:
            self.log.debug(
                "Kitsu max=%s for task %s (from products: %s)",
                max_kitsu_version,
                task_key,
                kitsu_instances,
            )
        else:
            self.log.debug(
                "No Kitsu revision for task %s (instances: %s)",
                task_key,
                [inst.data.get("productName", "unknown") for inst in instances],
            )

        # Get the highest AYON version across all instances in this task
        max_ayon_version = self._get_max_ayon_version_for_task(
            instances, project_name
        )

        # Determine the target version (highest of Kitsu or AYON + 1)
        target_version = max(max_kitsu_version, max_ayon_version) + 1

        self.log.debug(
            "Version sync detail %s: Kitsu=%s AYON=%s -> target v%s",
            task_key,
            max_kitsu_version,
            max_ayon_version,
            target_version,
        )
        self.log.info(
            "Kitsu version sync %s -> publish v%s",
            task_key,
            target_version,
        )

        # Apply the target version to all instances in this task group
        for instance in instances:
            original_version = instance.data.get("version")
            instance.data["version"] = target_version
            instance.data["versionSynced"] = True  # Mark as processed
            instance.data["kitsuTargetVersion"] = (
                target_version  # Store for PreserveSynchronizedVersions
            )

            # For grouped reviews, ensure all instances in the same task use the same version
            if "review" in instance.data.get("families", []):
                instance.data["kitsuGroupedVersion"] = target_version

            self.log.debug(
                "Synced %s: v%s -> v%s",
                instance.data.get("productName"),
                original_version,
                target_version,
            )

    def _get_max_ayon_version_for_task(self, instances, project_name):
        """Get the highest existing version from AYON across all instances in the task.

        Args:
            instances (list): List of instances for this task
            project_name (str): Project name

        Returns:
            int: Highest existing version number
        """
        try:
            import ayon_api

            # Collect all folder paths and product names from instances
            folder_paths = set()
            product_names_by_folder = {}
            skipped_incomplete = 0

            for instance in instances:
                product_name = instance.data.get("productName")
                folder_path = instance.data.get("folderPath")

                if not all([product_name, folder_path]):
                    skipped_incomplete += 1
                    continue

                folder_paths.add(folder_path)
                if folder_path not in product_names_by_folder:
                    product_names_by_folder[folder_path] = set()
                product_names_by_folder[folder_path].add(product_name)

            distinct_names = set()
            for names in product_names_by_folder.values():
                distinct_names.update(names)

            self.log.debug(
                "AYON max version: %s instances, %s folder paths, %s distinct "
                "product names (skipped incomplete: %s)",
                len(instances),
                len(folder_paths),
                len(distinct_names),
                skipped_incomplete,
            )

            if not folder_paths:
                self.log.debug("No valid folder paths for AYON version query")
                return 0

            folder_entities = list(
                ayon_api.get_folders(
                    project_name,
                    folder_paths=list(folder_paths),
                    fields={"id", "path"},
                )
            )

            if not folder_entities:
                self.log.debug("No folder entities resolved for AYON version query")
                return 0

            self.log.debug(
                "Resolved %s folder(s) for AYON product lookup",
                len(folder_entities),
            )

            path_to_folder_id = {
                folder["path"]: folder["id"] for folder in folder_entities
            }
            names_by_folder_ids = collections.defaultdict(set)
            unresolved_paths = []
            for folder_path, names in product_names_by_folder.items():
                folder_id = path_to_folder_id.get(folder_path)
                if folder_id:
                    names_by_folder_ids[folder_id].update(names)
                else:
                    unresolved_paths.append(folder_path)

            if unresolved_paths:
                self.log.debug(
                    "Folder path(s) not resolved (omitted from product query): %s",
                    unresolved_paths,
                )

            if not names_by_folder_ids:
                self.log.debug("No folder IDs mapped for AYON product query")
                return 0

            product_entities = list(
                ayon_api.get_products(
                    project_name,
                    names_by_folder_ids=dict(names_by_folder_ids),
                )
            )

            if not product_entities:
                self.log.info(
                    "No AYON products matched publish instance names and folders "
                    "(%s folder id(s) queried)",
                    len(names_by_folder_ids),
                )
                return 0

            self.log.debug(
                "Matched %s AYON product(s) for version lookup: %s",
                len(product_entities),
                [p["name"] for p in product_entities],
            )

            product_ids = [product["id"] for product in product_entities]

            # Get latest versions for all products at once
            last_versions = ayon_api.get_last_versions(
                project_name,
                product_ids,
                fields={"version", "productId"},
            )

            # Find the maximum version
            max_version = 0
            if last_versions:
                for product_id, version_data in last_versions.items():
                    if version_data is None:
                        continue
                    version_int = version_data.get("version", 0)
                    if version_int is not None and version_int > max_version:
                        max_version = version_int

            self.log.debug("Max AYON version: %s", max_version)
            return max_version

        except Exception:
            self.log.warning(
                "Failed to get AYON latest versions",
                exc_info=True,
            )

            # Fallback to checking instance data directly
            max_version = 0
            for instance in instances:
                current_version = instance.data.get("version", 0)
                if current_version > max_version:
                    max_version = current_version

            self.log.debug("Fallback max version from instance data: %s", max_version)
            return max_version

    def _get_version_start(self, instance, project_name):
        """Get version start value for an instance (fallback method).

        Args:
            instance: The instance to get version start for
            project_name (str): Project name

        Returns:
            int: Version start value
        """
        try:
            # Try to get from anatomy data
            anatomy_data = instance.data.get("anatomyData", {})
            version_start = anatomy_data.get("version_start", 1)
            return version_start
        except Exception:
            # Default fallback
            return 1
