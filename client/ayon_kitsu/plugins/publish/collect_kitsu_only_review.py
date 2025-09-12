# -*- coding: utf-8 -*-
"""Collection plugin for Kitsu-only review instances.

This plugin modifies the publish pipeline for instances that are marked
as Kitsu-only reviews to skip normal AYON integration steps.
"""
import pyblish.api

from ayon_kitsu.pipeline import KitsuPublishInstancePlugin


class CollectKitsuOnlyReview(KitsuPublishInstancePlugin):
    """Collect and configure Kitsu-only review instances.
    
    This plugin identifies instances that should only be submitted to Kitsu
    and configures them to skip normal AYON publishing workflow.
    """

    label = "Kitsu Only Review Setup"
    order = pyblish.api.CollectorOrder + 0.499
    families = ["kitsu"]

    def process(self, instance):
        """Process instances marked as Kitsu-only reviews."""
        
        # Only process instances marked as Kitsu-only reviews
        if not instance.data.get("kitsuOnlyReview", False):
            return
            
        self.log.info(f"Configuring Kitsu-only review: {instance.data.get('productName', 'Unnamed')}")
        
        # Skip AYON integration by removing integration-related families
        families = instance.data.get("families", [])
        
        # Remove families that would trigger AYON publishing
        families_to_remove = ["publish", "integrate"]
        for family in families_to_remove:
            if family in families:
                families.remove(family)
                self.log.debug(f"Removed '{family}' family to skip AYON integration")
        
        # Ensure we keep the kitsu family for Kitsu integration
        if "kitsu" not in families:
            families.append("kitsu")
            
        # Add a specific family for Kitsu-only processing
        if "kitsu_only" not in families:
            families.append("kitsu_only")
            
        instance.data["families"] = families
        
        # Set up minimal representation data from transient data if available
        representations = instance.data.get("representations", [])
        if not representations and hasattr(instance, 'transient_data') and "representations" in instance.transient_data:
            # Copy representation data from transient to main data for processing
            representations = instance.transient_data["representations"]
            instance.data["representations"] = representations
            self.log.debug("Copied representation data from transient storage")
            
        # Ensure representations have the kitsureview tag
        for representation in representations:
            tags = representation.get("tags", [])
            if "kitsureview" not in tags:
                tags.append("kitsureview")
                representation["tags"] = tags
                
        # Mark as not requiring AYON version creation
        instance.data["integrate"] = False
        instance.data["kitsuOnlyMode"] = True
        
        # Set a minimal version number if not already set
        if "version" not in instance.data:
            instance.data["version"] = 1
            
        self.log.debug(f"Configured instance families: {families}")
        self.log.debug(f"Representations: {len(representations)}")
        
        # Add context info for logging
        task_name = instance.data.get("task", "Unknown")
        folder_path = instance.data.get("folderPath", "Unknown")
        self.log.info(f"Kitsu-only review configured for {folder_path}/{task_name}")
