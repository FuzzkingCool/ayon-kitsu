# -*- coding: utf-8 -*-
import os

import pyblish.api
from ayon_core.pipeline import PublishError

from ayon_kitsu.addon import is_kitsu_enabled_in_settings
from ayon_kitsu.pipeline import KitsuPublishContextPlugin
from ayon_kitsu.utils import context_has_kitsu_family_instance


class KitsuLoginRepair(pyblish.api.Action):
    """Repair action to launch Kitsu login dialog."""

    label = "Login to Kitsu"
    icon = "key"
    on = "failed"

    def process(self, context, plugin):
        """Launch Kitsu login dialog."""
        try:
            from ayon_kitsu.kitsu_widgets import KitsuPasswordDialog

            dialog = KitsuPasswordDialog()
            result = dialog.exec_()

            if result:
                plugin.log.info("Kitsu login successful, please retry publish")
            else:
                plugin.log.warning("Kitsu login was cancelled")

        except Exception as e:
            plugin.log.error(f"Failed to show Kitsu login dialog: {e}")


class CollectKitsuLogin(KitsuPublishContextPlugin):
    """Collect Kitsu session using user credentials"""

    # After CollectKitsuFamily (0.4990) so we only log in when needed.
    order = pyblish.api.CollectorOrder + 0.4995
    label = "Kitsu user session"
    actions = [KitsuLoginRepair]
    # families = ["kitsu"]

    def process(self, context):
        project_settings = context.data["project_settings"]
        if not is_kitsu_enabled_in_settings(project_settings):
            self.log.info(
                f"Project '{context.data['projectName']} has disabled Kitsu"
            )
            return

        if not context_has_kitsu_family_instance(context):
            self.log.debug(
                "No instances with 'kitsu' family; skipping Kitsu login."
            )
            return

        # Check if credentials are available
        if not self._has_valid_credentials():
            raise PublishError(
                "Kitsu credentials not found. Please login to Kitsu using the repair action."
            )

        # Attempt to authenticate with Kitsu
        try:
            import gazu

            gazu.set_host(os.environ["KITSU_SERVER"])
            gazu.log_in(os.environ["KITSU_LOGIN"], os.environ["KITSU_PWD"])
            self.log.info("Successfully authenticated with Kitsu")
        except Exception as e:
            self.log.error(f"Kitsu authentication failed: {e}")
            raise PublishError(
                f"Kitsu authentication failed: {e}\n\n"
                "Please check your credentials and try the repair action to login again."
            )

    def _has_valid_credentials(self):
        """Check if Kitsu credentials are available in environment variables."""
        return (
            os.environ.get("KITSU_LOGIN")
            and os.environ.get("KITSU_PWD")
            and os.environ.get("KITSU_SERVER")
        )
