import os
import socket
import sys
import threading
import time

import ayon_api
import gazu
from nxtools import log_traceback, logging

from . import utils as processor_utils
from .fullsync import project_full_sync
from .comment_update import process_comment_update_request
from .update_from_kitsu import (
    create_or_update_asset,
    create_or_update_concept,
    create_or_update_edit,
    create_or_update_episode,
    create_or_update_person,
    create_or_update_sequence,
    create_or_update_shot,
    create_or_update_task,
    delete_asset,
    delete_concept,
    delete_edit,
    delete_episode,
    delete_person,
    delete_project,
    delete_sequence,
    delete_shot,
    delete_task,
    update_project,
)

if service_name := os.environ.get("AYON_SERVICE_NAME"):
    logging.user = service_name

SENDER = f"kitsu-processor-{socket.gethostname()}"


class KitsuServerError(Exception):
    pass


class KitsuSettingsError(Exception):
    pass


# Placeholder / invalid server values that must not be used (e.g. from UI defaults)
_INVALID_SERVER_PLACEHOLDERS = ("change.serverhost", "gazu.change.serverhost")


def _is_valid_server_url(server: str | None) -> bool:
    """Return False if server is missing or a known placeholder."""
    if not server or not (server := server.strip()):
        return False
    lower = server.lower()
    return not any(p in lower for p in _INVALID_SERVER_PLACEHOLDERS)


def _server_to_api_url(server: str | None) -> str:
    """Build Kitsu API URL from server setting; raises if invalid."""
    if not _is_valid_server_url(server):
        raise KitsuSettingsError(
            "Kitsu addon 'server' is not set or is a placeholder. "
            "Set a real Kitsu server URL in AYON addon settings (Studio)."
        )
    return server.rstrip("/") + "/api"


class KitsuProcessor:
    def __init__(self):
        logging.info("=" * 60)
        logging.info("KitsuProcessor.__init__ starting")
        logging.info("=" * 60)

        #
        # Connect to Ayon
        #
        logging.info("Step 1: Connecting to AYON server...")
        logging.info(
            f"AYON_SERVER_URL: {os.environ.get('AYON_SERVER_URL', 'NOT SET')}"
        )

        try:
            ayon_api.init_service()
            connected = True
            logging.info("Successfully connected to AYON server")
        except Exception as e:
            logging.error(f"Failed to connect to AYON server: {e}")
            log_traceback()
            connected = False

        if not connected:
            logging.error(
                "AYON connection failed, waiting 10 seconds before exit..."
            )
            time.sleep(10)
            print(
                "KitsuProcessor failed to connect to Ayon",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)

        #
        # Load settings and stuff...
        #
        logging.info("Step 2: Loading addon configuration...")

        self.addon_name = ayon_api.get_service_addon_name() or os.environ.get(
            "AYON_ADDON_NAME"
        )
        self.addon_version = (
            ayon_api.get_service_addon_version()
            or os.environ.get("AYON_ADDON_VERSION")
        )

        logging.info(f"Addon name: {self.addon_name}")
        logging.info(f"Addon version: {self.addon_version}")

        if not self.addon_name or not self.addon_version:
            logging.error("Addon name or version not set!")
            raise KitsuSettingsError(
                "AYON_ADDON_NAME or AYON_ADDON_VERSION not set. "
                "Ensure the processor service is properly configured in AYON or set "
                "AYON_ADDON_NAME and AYON_ADDON_VERSION environment variables."
            )

        self.entrypoint = f"/addons/{self.addon_name}/{self.addon_version}"
        logging.info(f"Addon endpoint: {self.entrypoint}")

        # Get settings from the correct addon version endpoint
        logging.info("Step 3: Fetching addon settings from AYON...")
        settings_endpoint = f"{self.entrypoint}/settings"
        logging.info(f"Settings endpoint: {settings_endpoint}")

        settings_res = ayon_api.get(settings_endpoint)
        logging.info(f"Settings response status: {settings_res.status_code}")

        if settings_res.status_code != 200:
            logging.error(f"Failed to fetch settings: {settings_res.detail}")
            raise KitsuSettingsError(
                f"Failed to fetch settings from {settings_endpoint}. "
                f"Status code {settings_res.status_code}: {settings_res.detail}"
            )

        self.settings = settings_res.data
        logging.info(
            f"Settings loaded successfully: {list(self.settings.keys())}"
        )

        #
        # Get list of projects that have been paired
        #
        logging.info("Step 4: Fetching project pairing list...")
        self.pairing_list = self.get_pairing_list()
        logging.info(f"Found {len(self.pairing_list)} paired projects")
        logging.debug(f"Pairing list: {self.pairing_list!r}")

        #
        # Get Kitsu server credentials from settings
        #
        logging.info("Step 5: Loading Kitsu credentials from settings...")
        try:
            kitsu_server_setting = self.settings.get("server")
            logging.info(f"Kitsu server setting: {kitsu_server_setting}")

            self.kitsu_server_url = _server_to_api_url(kitsu_server_setting)
            logging.info(f"Kitsu API URL: {self.kitsu_server_url}")

            email_secret = self.settings.get("login_email")
            password_secret = self.settings.get("login_password")

            logging.info(f"Email secret name: {email_secret}")
            logging.info(f"Password secret name: {password_secret}")

            if not email_secret:
                raise ValueError(f"Email secret `{email_secret}` not set")

            if not password_secret:
                raise ValueError(
                    f"Password secret `{password_secret}` not set"
                )

            logging.info("Fetching secrets from AYON...")
            try:
                email_data = ayon_api.get_secret(email_secret)
                self.kitsu_login_email = email_data["value"]
                logging.info(f"Email retrieved: {self.kitsu_login_email}")

                password_data = ayon_api.get_secret(password_secret)
                self.kitsu_login_password = password_data["value"]
                logging.info("Password retrieved successfully")
            except KeyError as e:
                logging.error(f"Secret key error: {e}")
                raise KitsuSettingsError(f"Secret `{e}` not found") from e
            except Exception as e:
                logging.error(f"Failed to retrieve secrets: {e}")
                raise

            assert self.kitsu_login_password, "Kitsu password not set"
            assert self.kitsu_server_url, "Kitsu server not set"
            assert self.kitsu_login_email, "Kitsu email not set"

            logging.info("All Kitsu credentials validated")

        except AssertionError as e:
            logging.error(f"Credential validation failed: {e}")
            raise KitsuSettingsError() from e

        #
        # Connect to Kitsu
        #
        logging.info(f"Connecting to Kitsu server: {self.kitsu_server_url}")
        gazu.set_host(self.kitsu_server_url)

        try:
            if not gazu.client.host_is_valid():
                raise KitsuServerError(
                    f"Kitsu server `{self.kitsu_server_url}` is not valid or unreachable. "
                    "Check network connectivity and server URL."
                )
        except Exception as e:
            logging.error(f"Failed to validate Kitsu server: {e}")
            raise KitsuServerError(
                f"Cannot reach Kitsu server at {self.kitsu_server_url}. "
                f"Error: {e}"
            ) from e

        try:
            logging.info(f"Logging in to Kitsu as {self.kitsu_login_email}")
            gazu.log_in(self.kitsu_login_email, self.kitsu_login_password)
            logging.info(
                f"Successfully logged in to Kitsu as {self.kitsu_login_email}"
            )
        except gazu.exception.AuthFailedException as e:
            logging.error(f"Kitsu authentication failed: {e}")
            raise KitsuServerError(
                f"Kitsu login failed for {self.kitsu_login_email}. "
                "Check credentials in AYON addon settings."
            ) from e
        except Exception as e:
            logging.error(f"Unexpected error during Kitsu login: {e}")
            raise KitsuServerError(f"Kitsu login error: {e}") from e

        # init event client
        self.kitsu_events_url = self.kitsu_server_url.replace(
            "api", "socket.io"
        )
        logging.info(
            f"Initializing Kitsu event client: {self.kitsu_events_url}"
        )
        gazu.set_event_host(self.kitsu_events_url)

        try:
            self.event_client = gazu.events.init()
            logging.info("Kitsu event client initialized successfully")
        except Exception as e:
            logging.error(f"Failed to initialize Kitsu event client: {e}")
            raise KitsuServerError(
                f"Cannot initialize Kitsu event client at {self.kitsu_events_url}. "
                f"Error: {e}"
            ) from e

        # Store host in thread-local storage for main thread
        logging.info("Step 7: Setting up thread-local Kitsu host...")
        processor_utils.set_kitsu_host(self.kitsu_server_url)
        logging.info("Thread-local Kitsu host configured")

        # ============= Add Kitsu Event Listeners ==============
        logging.info("Step 8: Starting Kitsu event listener thread...")
        self.gazu_listener_thread = threading.Thread(
            target=self.run_gazu_listeners,
            name="KitsuEventListener",
            daemon=False,
        )
        self.gazu_listener_thread.start()
        logging.info(
            f"Kitsu event listener thread started (alive: {self.gazu_listener_thread.is_alive()})"
        )

        logging.info("=" * 60)
        logging.info("KitsuProcessor initialization complete!")
        logging.info("=" * 60)

    def run_gazu_listeners(self):
        """Run Kitsu event listeners in a separate thread.

        This is a blocking call that runs forever, listening for Kitsu events.
        If this thread exits, the processor will no longer receive Kitsu events.
        """
        try:
            logging.info("Setting up Kitsu event listeners...")
            gazu.events.add_listener(
                self.event_client,
                "project:update",
                lambda data: update_project(self, data),
            )

            gazu.events.add_listener(
                self.event_client,
                "project:delete",
                lambda data: delete_project(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "asset:new",
                lambda data: create_or_update_asset(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "asset:update",
                lambda data: create_or_update_asset(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "asset:delete",
                lambda data: delete_asset(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "episode:new",
                lambda data: create_or_update_episode(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "episode:update",
                lambda data: create_or_update_episode(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "episode:delete",
                lambda data: delete_episode(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "sequence:new",
                lambda data: create_or_update_sequence(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "sequence:update",
                lambda data: create_or_update_sequence(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "sequence:delete",
                lambda data: delete_sequence(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "shot:new",
                lambda data: create_or_update_shot(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "shot:update",
                lambda data: create_or_update_shot(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "shot:delete",
                lambda data: delete_shot(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "task:new",
                lambda data: create_or_update_task(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "task:update",
                lambda data: create_or_update_task(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "task:delete",
                lambda data: delete_task(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "edit:new",
                lambda data: create_or_update_edit(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "edit:update",
                lambda data: create_or_update_edit(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "edit:delete",
                lambda data: delete_edit(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "person:new",
                lambda data: create_or_update_person(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "person:update",
                lambda data: create_or_update_person(self, data),
            )
            gazu.events.add_listener(
                self.event_client,
                "person:delete",
                lambda data: delete_person(self, data),
            )
            # Concept events were fixed in Zou 0.19.0, so listen only if
            # the user is running Zou euqual or above 0.19.0
            if tuple(gazu.client.get_api_version().split(".")) >= (
                "0",
                "19",
                "0",
            ):
                gazu.events.add_listener(
                    self.event_client,
                    "concept:new",
                    lambda data: create_or_update_concept(self, data),
                )
                gazu.events.add_listener(
                    self.event_client,
                    "concept:update",
                    lambda data: create_or_update_concept(self, data),
                )
                gazu.events.add_listener(
                    self.event_client,
                    "concept:delete",
                    lambda data: delete_concept(self, data),
                )

            logging.info("All Kitsu event listeners registered")
            logging.info("Starting Kitsu event client (blocking call)...")

            # This is a blocking call that runs forever
            gazu.events.run_client(self.event_client)

            # If we reach here, the event client stopped unexpectedly
            logging.error("Kitsu event client stopped unexpectedly!")

        except Exception as e:
            logging.error(f"Kitsu event listener thread crashed: {e}")
            log_traceback("Kitsu event listener thread error")
            raise

    def _resolve_kitsu_server_url(self) -> str:
        """Return the Kitsu API URL resolved at init from addon settings."""
        return self.kitsu_server_url

    def _ensure_gazu_host(self, url: str) -> None:
        """Set gazu client host to the given Kitsu API URL and verify it stuck.

        Gazu's default_client is created with host 'http://gazu.change.serverhost/api';
        if set_host is not applied before a request, that default is used.
        """
        if "change.serverhost" in url.lower():
            raise KitsuSettingsError(
                "Refusing to set gazu host to placeholder. "
                "Set a real Kitsu server URL in AYON addon settings (Studio)."
            )
        gazu.set_host(url)
        actual = gazu.get_host()
        if actual != url:
            raise KitsuSettingsError(
                f"Gazu host did not stick: set {url!r}, got {actual!r}. "
                "Check for code that resets gazu.client."
            )

    def get_pairing_list(self):
        """maintain a list of pairings so that we can check
        the kitsu change is in a paired project and get the ayon project name
        """
        logging.info(
            f"get_pairing_list from endpoint: {self.entrypoint}/pairing"
        )
        logging.info(
            f"Using addon version: {self.addon_name}/{self.addon_version}"
        )
        res = ayon_api.get(f"{self.entrypoint}/pairing")

        if res.status_code != 200:
            logging.error(
                f"Failed to fetch pairing list from {self.entrypoint}/pairing. "
                f"Status: {res.status_code}, Detail: {res.detail}"
            )
            logging.error(
                f"This likely means addon version {self.addon_version} is not deployed "
                "in the current bundle. Check AYON_ADDON_NAME and AYON_ADDON_VERSION "
                "environment variables match the deployed addon version."
            )
            # Try to get available addon versions for diagnostics
            try:
                addons_res = ayon_api.get("/api/addons")
                if addons_res.status_code == 200:
                    available = [
                        f"{name}/{ver}"
                        for name, versions in addons_res.data.get(
                            "addons", {}
                        ).items()
                        for ver in versions.keys()
                        if name == self.addon_name
                    ]
                    logging.error(
                        f"Available {self.addon_name} versions: {available}"
                    )
            except Exception as e:
                logging.error(f"Could not fetch available addon versions: {e}")

            raise KitsuSettingsError(
                f"Pairing endpoint failed with status {res.status_code}. "
                f"Ensure addon {self.addon_name}/{self.addon_version} is deployed."
            )

        logging.debug(f"Pairing list response data: {res.data!r}")

        # If empty, try production bundle as fallback
        if not res.data:
            logging.warning(
                f"Pairing endpoint returned empty from {self.entrypoint}/pairing. "
                "Attempting to fetch from production bundle as fallback."
            )

            # Get production bundle info to find the Kitsu addon version
            try:
                bundles_res = ayon_api.get("/bundles")
                if bundles_res.status_code == 200:
                    production_bundle_name = bundles_res.data.get(
                        "productionBundle"
                    )
                    if production_bundle_name:
                        logging.info(
                            f"Production bundle: {production_bundle_name}"
                        )

                        # Get the bundle details to find Kitsu addon version
                        bundles_list = bundles_res.data.get("bundles", [])
                        production_bundle = next(
                            (
                                b
                                for b in bundles_list
                                if b["name"] == production_bundle_name
                            ),
                            None,
                        )

                        if production_bundle:
                            production_kitsu_version = production_bundle.get(
                                "addons", {}
                            ).get(self.addon_name)
                            if production_kitsu_version:
                                logging.info(
                                    f"Found production Kitsu version: {production_kitsu_version}"
                                )
                                production_endpoint = f"/addons/{self.addon_name}/{production_kitsu_version}/pairing"
                                logging.info(
                                    f"Trying production endpoint: {production_endpoint}"
                                )
                                prod_res = ayon_api.get(production_endpoint)

                                if (
                                    prod_res.status_code == 200
                                    and prod_res.data
                                ):
                                    logging.info(
                                        f"Successfully fetched {len(prod_res.data)} pairings "
                                        f"from production bundle (version {production_kitsu_version})"
                                    )
                                    return prod_res.data
                                else:
                                    logging.warning(
                                        f"Production endpoint returned empty or failed. "
                                        f"Status: {prod_res.status_code}"
                                    )
                            else:
                                logging.warning(
                                    f"Kitsu addon not found in production bundle {production_bundle_name}"
                                )
                        else:
                            logging.warning(
                                "Could not find production bundle details"
                            )
                    else:
                        logging.warning("No production bundle configured")
                else:
                    logging.warning(
                        f"Failed to fetch bundles list: {bundles_res.status_code}"
                    )
            except Exception as e:
                logging.error(f"Error fetching production bundle info: {e}")

        return res.data

    def get_paired_ayon_project(self, kitsu_project_id: str) -> str | None:
        """returns the ayon project if paired else None"""
        for pair in self.pairing_list:
            if pair["kitsuProjectId"] == kitsu_project_id:
                return pair["ayonProjectName"]

    def set_paired_ayon_project(
        self, kitsu_project_id: str, ayon_project_name: str
    ):
        """add a new pair to the list"""
        for pair in self.pairing_list:
            if "kitsuProjectId" in pair:
                return
        self.pairing_list.append(
            {
                "kitsuProjectId": kitsu_project_id,
                "ayonProjectName": ayon_project_name,
            }
        )

    def start_processing(self):
        logging.info("=" * 60)
        logging.info("START PROCESSING LOOP")
        logging.info("=" * 60)
        startup = True
        loop_count = 0
        last_status_log = time.time()

        while True:
            loop_count += 1

            # Log status every 60 seconds
            current_time = time.time()
            if current_time - last_status_log >= 60:
                logging.info(
                    f"Status: Loop iteration {loop_count}, thread alive: {self.gazu_listener_thread.is_alive()}"
                )
                last_status_log = current_time

            # Check if event listener thread is still alive
            if not self.gazu_listener_thread.is_alive():
                logging.error(
                    "FATAL: Kitsu event listener thread has died! "
                    "The processor will no longer receive Kitsu events."
                )
                raise RuntimeError("Kitsu event listener thread died")

            # Sync all paired projects
            if startup:
                logging.info("Running sync for all paired projects")
                try:
                    for pair in self.pairing_list:
                        project_id = pair.get("kitsuProjectId")
                        project_name = pair.get("ayonProjectName")
                        if project_id and project_name:
                            logging.info(
                                f"Syncing project: {project_name} (Kitsu ID: {project_id})"
                            )
                            project_full_sync(
                                self,
                                project_id,
                                project_name,
                            )
                    logging.info(
                        "Initial sync completed for all paired projects"
                    )
                except Exception as e:
                    logging.error(f"Error during startup sync: {e}")
                    log_traceback("Startup sync error")
                    # Don't crash on startup sync errors, just log and continue
                finally:
                    startup = False

            # Check for comment update job first (uniqueSprites bubble-up)
            job = ayon_api.enroll_event_job(
                source_topic="kitsu.comment_update_request",
                target_topic="addon.kitsu.processor.comment_update",
                sender=SENDER,
                description="Update Kitsu comment with uniqueSprites",
                max_retries=2,
            )
            if job:
                src_ev = ayon_api.get_event(job["dependsOn"])
                project_name = src_ev.get("project") or ""
                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="in_progress",
                    project_name=project_name,
                    description="Updating Kitsu comment...",
                )
                try:
                    process_comment_update_request(self, src_ev)
                except Exception:
                    log_traceback("Comment update error")
                    ayon_api.update_event(
                        job["id"],
                        sender=SENDER,
                        status="failed",
                        project_name=project_name,
                        description="Comment update failed",
                    )
                else:
                    ayon_api.update_event(
                        job["id"],
                        sender=SENDER,
                        status="finished",
                        project_name=project_name,
                        description="Kitsu comment updated",
                    )
                continue

            # Check for sync job
            job = ayon_api.enroll_event_job(
                source_topic="kitsu.sync_request",
                target_topic="kitsu.sync",
                sender=SENDER,
                description="Syncing Kitsu to Ayon",
                max_retries=3,
            )

            if not job:
                time.sleep(5)
                continue

            src_job = ayon_api.get_event(job["dependsOn"])

            kitsu_project_id = src_job["summary"]["kitsuProjectId"]
            ayon_project_name = src_job["project"]

            ayon_api.update_event(
                job["id"],
                sender=SENDER,
                status="in_progress",
                project_name=ayon_project_name,
                description="Syncing Kitsu project...",
            )

            try:
                project_full_sync(self, kitsu_project_id, ayon_project_name)

                # if successful add the pair to the list
                self.set_paired_ayon_project(
                    kitsu_project_id, ayon_project_name
                )
            except Exception:
                log_traceback(
                    f"Unable to sync kitsu project {ayon_project_name}"
                )

                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="failed",
                    project_name=ayon_project_name,
                    description="Sync failed",
                )
            else:
                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="finished",
                    project_name=ayon_project_name,
                    description="Kitsu sync finished",
                )

        logging.info("KitsuProcessor finished processing")
        gazu.log_out()
