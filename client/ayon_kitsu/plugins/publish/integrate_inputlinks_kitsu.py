# -*- coding: utf-8 -*-
"""Custom InputLinks integration plugin for Kitsu addon.

This plugin extends the core IntegrateInputLinksAYON plugin to handle
Kitsu-specific instances that don't require workfile linking.
"""
import pyblish.api

# Import the core plugin
try:
    from ayon_core.plugins.publish.integrate_inputlinks import IntegrateInputLinksAYON
except ImportError:
    # Fallback if import path changes
    IntegrateInputLinksAYON = None


class IntegrateInputLinksKitsu(IntegrateInputLinksAYON):
    """Custom InputLinks integration for Kitsu addon.
    
    This plugin extends the core IntegrateInputLinksAYON plugin to suppress
    the "No workfile in this publish session" warning when publishing
    Kitsu-only review instances that don't require workfile linking.
    """

    # Higher order to override the core plugin
    order = pyblish.api.IntegratorOrder + 0.21
    label = "Connect Dependency InputLinks AYON (Kitsu)"

    def create_workfile_links(
        self, workfile_instance, other_instances, new_links_by_type
    ):
        """Adds links (generative and reference) for workfile.

        Extends the core method to handle Kitsu-specific instances.
        """
        if workfile_instance is None:
            # Check if we have Kitsu-only instances that don't need workfile linking
            if self._has_kitsu_standalone_instances(other_instances):
                self.log.debug(
                    "No workfile in this publish session, but found Kitsu "
                    "standalone instances that don't require workfile linking."
                )
            else:
                self.log.warning("No workfile in this publish session.")
            return

        # Use the parent method for normal processing when workfile exists
        super(IntegrateInputLinksKitsu, self).create_workfile_links(
            workfile_instance, other_instances, new_links_by_type
        )

    def _has_kitsu_standalone_instances(self, instances):
        """Check if we have Kitsu instances that don't require workfile linking.
        
        Args:
            instances (list[pyblish.plugin.Instance]): Instances to check.
            
        Returns:
            bool: True if any instance is a Kitsu standalone type.
        """
        for instance in instances:
            # Check for Kitsu-only review instances
            if instance.data.get("kitsuOnlyReview", False):
                return True
            
            # Check for other Kitsu standalone instance types
            families = instance.data.get("families", [])
            if ("kitsu" in families and 
                instance.data.get("productType") == "review" and
                instance.data.get("kitsuOnlyMode", False)):
                return True
                
        return False


# Only register if the core plugin is available
if IntegrateInputLinksAYON is not None:
    # This will be registered and override the core plugin due to higher order
    pass
else:
    # If core plugin not available, disable this plugin
    class IntegrateInputLinksKitsu:
        active = False
