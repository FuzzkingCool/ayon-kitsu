# -*- coding: utf-8 -*-
import os

import pyblish.api
from ayon_core.pipeline import PublishXmlValidationError
from ayon_kitsu.pipeline import KitsuPublishContextPlugin


class CollectKitsuLogin(KitsuPublishContextPlugin):
    """Collect Kitsu session using user credentials"""

    order = pyblish.api.CollectorOrder
    label = "Kitsu user session"
    # families = ["kitsu"]

    def process(self, context):
        # Check for required environment variables first
        missing_vars = []
        if not os.environ.get("KITSU_LOGIN"):
            missing_vars.append("KITSU_LOGIN")
        if not os.environ.get("KITSU_PWD"):
            missing_vars.append("KITSU_PWD")
        if not os.environ.get("KITSU_SERVER"):
            missing_vars.append("KITSU_SERVER")

        if missing_vars:
            self.log.error(f"Missing Kitsu environment variables: {missing_vars}")
            raise PublishXmlValidationError(
                self,
                "You are not properly logged into Kitsu through AYON.",
                key="main"
            )

        # Attempt to authenticate with Kitsu
        try:
            import gazu
            gazu.set_host(os.environ["KITSU_SERVER"])
            gazu.log_in(os.environ["KITSU_LOGIN"], os.environ["KITSU_PWD"])
            self.log.info("Successfully authenticated with Kitsu")
        except Exception as e:
            # Convert any authentication errors to user-friendly message
            self.log.error(f"Kitsu authentication failed: {e}")
            raise PublishXmlValidationError(
                self,
                "You are not properly logged into Kitsu through AYON.",
                key="main"
            )
