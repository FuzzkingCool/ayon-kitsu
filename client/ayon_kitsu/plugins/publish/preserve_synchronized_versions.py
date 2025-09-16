"""Preserve synchronized versions after CollectAnatomyInstanceData.

This plugin runs after CollectAnatomyInstanceData to restore versions that were
synchronized by SyncAllVersionsWithKitsu but may have been overridden by the
core anatomy collection plugin.
"""

import pyblish.api
from ayon_kitsu.pipeline import KitsuPublishContextPlugin
from ayon_harmony.logger import log as log_harmony


class PreserveSynchronizedVersions(KitsuPublishContextPlugin):
    """Preserve versions that were synchronized by SyncAllVersionsWithKitsu.

    This plugin runs after CollectAnatomyInstanceData to ensure that any
    versions synchronized by SyncAllVersionsWithKitsu are preserved and not
    overridden by the core anatomy collection logic.
    """

    label = "Preserve Synchronized Versions"
    order = pyblish.api.CollectorOrder + 0.51  # Run after CollectAnatomyInstanceData (0.49)
    # No hosts restriction - this works for ALL hosts

    log = log_harmony
    log.info("PreserveSynchronizedVersions plugin loaded")

    def process(self, context):
        """Process all instances and restore synchronized versions if needed."""
        self.log.info("PreserveSynchronizedVersions plugin starting...")
        project_name = context.data.get("projectName")
        if not project_name:
            self.log.warning(
                "No project name found, skipping synchronized version preservation"
            )
            return

        # Process all instances to restore synchronized versions
        instances_restored = 0
        total_instances = 0
        synced_instances = 0
        
        for instance in context:
            total_instances += 1
            if instance.data.get("versionSynced"):
                synced_instances += 1
                self.log.debug(f"Found synced instance: {instance.data.get('productName')} (version={instance.data.get('version')})")
            
            if self._restore_synchronized_version(instance):
                instances_restored += 1

        self.log.info(f"PreserveSynchronizedVersions: {total_instances} total instances, {synced_instances} synced instances, {instances_restored} restored")
        
        if instances_restored > 0:
            self.log.info(
                f"Restored synchronized versions for {instances_restored} instances"
            )
        else:
            self.log.debug("No instances needed synchronized version restoration")

    def _restore_synchronized_version(self, instance):
        """Restore synchronized version for an instance if needed.

        Args:
            instance: The instance to check and potentially restore

        Returns:
            bool: True if version was restored, False otherwise
        """
        product_name = instance.data.get('productName', 'unknown')
        
        # Only process instances that were synchronized
        if not instance.data.get("versionSynced"):
            self.log.debug(f"Instance {product_name} not versionSynced, skipping")
            return False

        # Get the synchronized version that was set by SyncAllVersionsWithKitsu
        # We need to store this before CollectAnatomyInstanceData overwrites it
        synchronized_version = instance.data.get("kitsuTargetVersion")
        if synchronized_version is None:
            # Fallback: use the current version if no stored target version
            synchronized_version = instance.data.get("version")

        if synchronized_version is None:
            self.log.warning(
                f"No synchronized version found for {product_name}"
            )
            return False

        # Check if the version was overridden by CollectAnatomyInstanceData
        current_version = instance.data.get("version")
        
        self.log.debug(f"Instance {product_name}: synchronized_version={synchronized_version}, current_version={current_version}")

        # If current version differs from synchronized version, restore it
        if current_version != synchronized_version:
            self.log.info(
                f"Restoring synchronized version {synchronized_version} for "
                f"{product_name} (was overridden to {current_version})"
            )

            # Update both instance version and anatomy data
            instance.data["version"] = synchronized_version
            if "anatomyData" in instance.data:
                instance.data["anatomyData"]["version"] = synchronized_version

            return True

        self.log.debug(f"Instance {product_name} version already correct, no restoration needed")
        return False
