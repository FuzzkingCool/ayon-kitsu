"""Universal version synchronization with Kitsu for all product types and hosts."""

import pyblish.api
from ayon_harmony.logger import log as log_harmony

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

    log = log_harmony
    log.info("SyncAllVersionsWithKitsu plugin loaded")

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

        self.log.info(
            f"Found {len(instances_to_sync)} instances for Kitsu version synchronization"
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
            "harmony.tbg",
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
        self.log.info(
            f"Synchronizing versions for task group: {task_key} ({len(instances)} instances)"
        )

        # Get the highest Kitsu version across all instances in this task
        max_kitsu_version = 0
        kitsu_instances = []

        self.log.debug(
            f"Checking Kitsu versions for {len(instances)} instances in task {task_key}"
        )

        for instance in instances:
            kitsu_latest = instance.data.get("kitsuLatestVersion", 0)
            product_name = instance.data.get("productName", "unknown")

            self.log.debug(
                f"Instance {product_name}: kitsuLatestVersion={kitsu_latest}"
            )

            if kitsu_latest and kitsu_latest > max_kitsu_version:
                max_kitsu_version = kitsu_latest

            if kitsu_latest:
                kitsu_instances.append(product_name)

        if max_kitsu_version > 0:
            self.log.info(
                f"Found Kitsu latest version {max_kitsu_version} for task {task_key} "
                f"from instances: {kitsu_instances}"
            )
        else:
            self.log.info(
                f"No Kitsu version found for task {task_key}. "
                f"Instances checked: {[inst.data.get('productName', 'unknown') for inst in instances]}"
            )

        # Get the highest AYON version across all instances in this task
        max_ayon_version = self._get_max_ayon_version_for_task(
            instances, project_name
        )

        # Determine the target version (highest of Kitsu or AYON + 1)
        target_version = max(max_kitsu_version, max_ayon_version) + 1

        self.log.info(
            f"Version sync for {task_key}: Kitsu={max_kitsu_version}, "
            f"AYON={max_ayon_version}, Target={target_version} "
            f"(using max of both + 1)"
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

            self.log.info(
                f"Synced {instance.data.get('productName')}: "
                f"v{original_version} -> v{target_version}"
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

            self.log.debug("Collecting product names from instances:")
            for instance in instances:
                product_name = instance.data.get("productName")
                folder_path = instance.data.get("folderPath")
                product_type = instance.data.get("productType")

                self.log.debug(
                    f"  Instance: {product_name} (type: {product_type}) in {folder_path}"
                )

                if not all([product_name, folder_path]):
                    self.log.debug(
                        f"  Skipping instance with missing data: product={product_name}, folder={folder_path}"
                    )
                    continue

                folder_paths.add(folder_path)
                if folder_path not in product_names_by_folder:
                    product_names_by_folder[folder_path] = set()
                product_names_by_folder[folder_path].add(product_name)

            if not folder_paths:
                self.log.debug(
                    "No valid folder paths found for AYON version query"
                )
                return 0

            # Get folder entities
            self.log.debug(f"Querying folders for paths: {list(folder_paths)}")
            folder_entities = list(
                ayon_api.get_folders(
                    project_name,
                    folder_paths=list(folder_paths),
                    fields={"id", "path"},
                )
            )

            if not folder_entities:
                self.log.debug(
                    "No folder entities found for AYON version query"
                )
                return 0

            folder_ids = [folder["id"] for folder in folder_entities]
            folder_paths_list = [folder["path"] for folder in folder_entities]
            self.log.debug(
                f"Found {len(folder_ids)} folder entities: {folder_paths_list}"
            )

            # Create a mapping from folder_id to folder_path for lookup
            folder_id_to_path = {
                folder["id"]: folder["path"] for folder in folder_entities
            }

            # Use a simpler approach: get all products for these folders, then filter by name
            all_products = list(
                ayon_api.get_products(
                    project_name,
                    folder_ids=folder_ids,
                    fields={"id", "name", "folderId"},
                )
            )

            if not all_products:
                self.log.debug("No products found for AYON version query")
                return 0

            self.log.debug(
                f"Found {len(all_products)} total products in folders"
            )

            # Filter products by name and folder
            valid_products = []
            self.log.debug(
                f"Looking for products in folders: {list(product_names_by_folder.keys())}"
            )
            self.log.debug(
                f"Looking for product names: {[list(names) for names in product_names_by_folder.values()]}"
            )

            for product in all_products:
                product_name = product["name"]
                folder_id = product["folderId"]

                # Find which folder path this corresponds to using the mapping
                folder_path = folder_id_to_path.get(folder_id)

                self.log.debug(
                    f"  Checking product: {product_name} in folder_id={folder_id} (path={folder_path})"
                )

                if folder_path and folder_path in product_names_by_folder:
                    expected_names = product_names_by_folder[folder_path]
                    self.log.debug(
                        f"    Expected names for {folder_path}: {list(expected_names)}"
                    )
                    if product_name in expected_names:
                        valid_products.append(product)
                        self.log.debug(
                            f"    ✅ Valid product: {product_name} in {folder_path}"
                        )
                    else:
                        self.log.debug(
                            f"    ❌ Product name '{product_name}' not in expected names: {list(expected_names)}"
                        )
                else:
                    self.log.debug(
                        f"    ❌ Folder path {folder_path} not found in expected folders"
                    )

            if not valid_products:
                self.log.info(
                    "No valid products found after filtering by name and folder"
                )
                return 0

            valid_product_ids = [product["id"] for product in valid_products]
            self.log.debug(
                f"Found {len(valid_product_ids)} valid products: {[p['name'] for p in valid_products]}"
            )

            # Get latest versions for all products at once
            last_versions = ayon_api.get_last_versions(
                project_name,
                valid_product_ids,
                fields={"version", "productId"},
            )

            # Find the maximum version
            max_version = 0
            for product_id, version_data in last_versions.items():
                version_int = version_data.get("version", 0)
                if version_int > max_version:
                    max_version = version_int

            self.log.debug(f"Max AYON version found: {max_version}")
            return max_version

        except Exception as e:
            self.log.warning(f"Failed to get AYON latest versions: {e}")
            import traceback

            self.log.debug(traceback.format_exc())

            # Fallback to checking instance data directly
            max_version = 0
            for instance in instances:
                current_version = instance.data.get("version", 0)
                if current_version > max_version:
                    max_version = current_version

            self.log.debug(
                f"Fallback max version from instances: {max_version}"
            )
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
