import os
import socket
import sys
import threading
import time

import ayon_api
import gazu
from nxtools import log_traceback, logging

from . import utils as processor_utils
from .ayon_event_loop import run_ayon_event_loop
from .content_sync import (
    delete_comment_from_ayon,
    log_ayon_version_author_update_capability,
    sync_comment_to_ayon,
    sync_preview_to_ayon,
    update_comment_on_ayon,
)
from .fullsync import project_full_sync
from .kitsu_socket_dispatch import KitsuSocketLaneDispatcher, add_listener_laned
from .pairing_fallback import (
    pairing_http_error_is_kitsu_login,
    pairing_list_from_processor_session,
)
from .update_from_kitsu import (
    create_or_update_asset,
    create_or_update_concept,
    create_or_update_edit,
    create_or_update_episode,
    create_or_update_person,
    create_or_update_playlist,
    create_or_update_sequence,
    create_or_update_shot,
    create_or_update_task,
    delete_asset,
    delete_concept,
    delete_edit,
    delete_episode,
    delete_person,
    delete_playlist,
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


def _resolve_kitsu_api_url(settings_server: str | None) -> str:
    """Prefer addon settings, then KITSU_SERVER / KITSU_URL (local dev)."""
    sources: tuple[tuple[str, str | None], ...] = (
        ("addon settings", settings_server),
        ("KITSU_SERVER env", os.environ.get("KITSU_SERVER")),
        ("KITSU_URL env", os.environ.get("KITSU_URL")),
    )
    for label, raw in sources:
        if not _is_valid_server_url(raw):
            continue
        logging.info(f"Using Kitsu server from {label}")
        return raw.rstrip("/") + "/api"
    raise KitsuSettingsError(
        "Kitsu server URL is missing or a placeholder in addon settings, "
        "and KITSU_SERVER / KITSU_URL are not set to a valid URL. "
        "Set Server in AYON Studio (Kitsu addon), or set KITSU_SERVER for local dev."
    )


def _kitsu_login_from_env() -> tuple[str, str] | None:
    """Return (email, password) from env if both are set; else None."""
    email = (os.environ.get("KITSU_LOGIN") or os.environ.get("KITSU_EMAIL") or "").strip()
    password = (os.environ.get("KITSU_PWD") or "").strip()
    if email and password:
        return email, password
    return None


class _DisabledProcessorListenerThread:
    """Placeholder when Kitsu Socket.IO / AYON event threads are not started."""

    __slots__ = ()

    def is_alive(self) -> bool:
        return False


class KitsuProcessor:
    def __init__(self, *, start_listener_threads: bool = True):
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
            log_ayon_version_author_update_capability()
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
        # Get Kitsu server credentials from settings
        #
        logging.info("Step 4: Loading Kitsu credentials from settings...")
        try:
            kitsu_server_setting = self.settings.get("server")
            logging.info(f"Kitsu server setting (addon): {kitsu_server_setting}")

            self.kitsu_server_url = _resolve_kitsu_api_url(kitsu_server_setting)
            logging.info(f"Kitsu API URL: {self.kitsu_server_url}")

            env_creds = _kitsu_login_from_env()
            if env_creds:
                self.kitsu_login_email, self.kitsu_login_password = env_creds
                self._kitsu_credentials_source = "environment"
                logging.info(
                    "Using Kitsu credentials from environment "
                    "(KITSU_LOGIN or KITSU_EMAIL, plus KITSU_PWD)"
                )
            else:
                self._kitsu_credentials_source = "addon_secrets"
                email_secret = self.settings.get("login_email")
                password_secret = self.settings.get("login_password")

                logging.info(f"Email secret name: {email_secret}")
                logging.info(f"Password secret name: {password_secret}")

                if not email_secret:
                    raise ValueError(
                        "login_email secret is not set in addon settings. "
                        "Configure it in AYON Studio, or set KITSU_LOGIN (or KITSU_EMAIL) "
                        "and KITSU_PWD in the environment for local development."
                    )

                if not password_secret:
                    raise ValueError(
                        "login_password secret is not set in addon settings. "
                        "Configure it in AYON Studio, or set KITSU_LOGIN (or KITSU_EMAIL) "
                        "and KITSU_PWD in the environment for local development."
                    )

                logging.info("Fetching secrets from AYON...")
                try:
                    email_data = ayon_api.get_secret(email_secret)
                    self.kitsu_login_email = email_data["value"]
                    logging.info(f"Email retrieved: {self.kitsu_login_email}")

                    password_data = ayon_api.get_secret(password_secret)
                    self.kitsu_login_password = password_data["value"]
                    logging.info(
                        "Using Kitsu credentials from AYON Studio secrets "
                        "(login_email / login_password); set KITSU_* env to override locally."
                    )
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
            src = getattr(self, "_kitsu_credentials_source", "unknown")
            if src == "environment":
                hint = (
                    "Verify KITSU_PWD for this email, KITSU_SERVER / addon server URL, "
                    "and that the local driver merged the intended .env (see test_processor_image)."
                )
            else:
                hint = (
                    "Verify AYON Studio secrets for login_email / login_password, "
                    "or set KITSU_LOGIN (or KITSU_EMAIL) and KITSU_PWD to override for local runs."
                )
            raise KitsuServerError(
                f"Kitsu login failed for {self.kitsu_login_email}. "
                f"Credential source: {src}. {hint}"
            ) from e
        except Exception as e:
            logging.error(f"Unexpected error during Kitsu login: {e}")
            raise KitsuServerError(f"Kitsu login error: {e}") from e

        self.start_listener_threads = start_listener_threads
        self.kitsu_events_url = None
        self.event_client = None
        self.socket_lane_dispatcher = None

        if start_listener_threads:
            # init event client (Socket.IO); not needed for one-shot fullsync drivers.
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
            self.socket_lane_dispatcher = KitsuSocketLaneDispatcher(self)
            self.socket_lane_dispatcher.start()
        else:
            logging.info(
                "Kitsu Socket.IO event client skipped (start_listener_threads=False)"
            )

        # Store host in thread-local storage for main thread
        logging.info("Step 7: Setting up thread-local Kitsu host...")
        processor_utils.set_kitsu_host(self.kitsu_server_url)
        logging.info("Thread-local Kitsu host configured")

        logging.info(
            "Fetching project pairing list (GET /pairing on AYON uses Studio "
            "Kitsu secrets; see pairing_fallback if that login fails)."
        )
        self.pairing_list = self.get_pairing_list()
        logging.info(f"Found {len(self.pairing_list)} rows in pairing list")
        logging.debug(f"Pairing list: {self.pairing_list!r}")

        if start_listener_threads:
            # ============= Add Kitsu Event Listeners ==============
            logging.info("Step 8: Starting Kitsu event listener thread...")
            self.gazu_listener_thread = threading.Thread(
                target=self.run_gazu_listeners,
                name="KitsuEventListener",
                daemon=False,
            )
            self.gazu_listener_thread.start()
            logging.info(
                "Kitsu event listener thread started "
                f"(alive: {self.gazu_listener_thread.is_alive()})"
            )

            # ============= AYON event enrollment thread ==============
            logging.info("Step 9: Starting AYON event loop thread...")
            self.ayon_event_thread = threading.Thread(
                target=run_ayon_event_loop,
                args=(self,),
                name="AyonEventLoop",
                daemon=False,
            )
            self.ayon_event_thread.start()
            logging.info(
                "AYON event loop thread started "
                f"(alive: {self.ayon_event_thread.is_alive()})"
            )
        else:
            logging.info(
                "Steps 8-9: Kitsu listener and AYON event threads not started "
                "(start_listener_threads=False; do not call start_processing)"
            )
            disabled = _DisabledProcessorListenerThread()
            self.gazu_listener_thread = disabled
            self.ayon_event_thread = disabled

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
            add_listener_laned(
                self,
                self.event_client,
                "project:update",
                lambda data: update_project(self, data),
                lane="fast",
            )

            add_listener_laned(
                self,
                self.event_client,
                "project:delete",
                lambda data: delete_project(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "asset:new",
                lambda data: create_or_update_asset(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "asset:update",
                lambda data: create_or_update_asset(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "asset:delete",
                lambda data: delete_asset(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "episode:new",
                lambda data: create_or_update_episode(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "episode:update",
                lambda data: create_or_update_episode(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "episode:delete",
                lambda data: delete_episode(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "sequence:new",
                lambda data: create_or_update_sequence(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "sequence:update",
                lambda data: create_or_update_sequence(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "sequence:delete",
                lambda data: delete_sequence(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "shot:new",
                lambda data: create_or_update_shot(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "shot:update",
                lambda data: create_or_update_shot(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "shot:delete",
                lambda data: delete_shot(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "task:new",
                lambda data: create_or_update_task(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "task:update",
                lambda data: create_or_update_task(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "task:delete",
                lambda data: delete_task(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "playlist:new",
                lambda data: create_or_update_playlist(self, data),
                lane="slow",
            )
            add_listener_laned(
                self,
                self.event_client,
                "playlist:update",
                lambda data: create_or_update_playlist(self, data),
                lane="slow",
            )
            add_listener_laned(
                self,
                self.event_client,
                "playlist:delete",
                lambda data: delete_playlist(self, data),
                lane="slow",
            )
            add_listener_laned(
                self,
                self.event_client,
                "edit:new",
                lambda data: create_or_update_edit(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "edit:update",
                lambda data: create_or_update_edit(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "edit:delete",
                lambda data: delete_edit(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "person:new",
                lambda data: create_or_update_person(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "person:update",
                lambda data: create_or_update_person(self, data),
                lane="fast",
            )
            add_listener_laned(
                self,
                self.event_client,
                "person:delete",
                lambda data: delete_person(self, data),
                lane="fast",
            )
            # Concept events were fixed in Zou 0.19.0, so listen only if
            # the user is running Zou euqual or above 0.19.0
            if tuple(gazu.client.get_api_version().split(".")) >= (
                "0",
                "19",
                "0",
            ):
                add_listener_laned(
                    self,
                    self.event_client,
                    "concept:new",
                    lambda data: create_or_update_concept(self, data),
                    lane="fast",
                )
                add_listener_laned(
                    self,
                    self.event_client,
                    "concept:update",
                    lambda data: create_or_update_concept(self, data),
                    lane="fast",
                )
                add_listener_laned(
                    self,
                    self.event_client,
                    "concept:delete",
                    lambda data: delete_concept(self, data),
                    lane="fast",
                )

            # Content sync listeners: comments and preview files (slow lane)
            add_listener_laned(
                self,
                self.event_client,
                "comment:new",
                lambda data: sync_comment_to_ayon(
                    self, data.get("comment_id", ""),
                    data.get("task_id", ""), data.get("project_id", ""),
                ),
                lane="slow",
            )
            add_listener_laned(
                self,
                self.event_client,
                "comment:update",
                lambda data: update_comment_on_ayon(
                    self, data.get("comment_id", ""),
                    data.get("task_id", ""), data.get("project_id", ""),
                ),
                lane="slow",
            )
            add_listener_laned(
                self,
                self.event_client,
                "comment:delete",
                lambda data: delete_comment_from_ayon(
                    self, data.get("comment_id", ""),
                    data.get("task_id", ""), data.get("project_id", ""),
                ),
                lane="slow",
            )
            add_listener_laned(
                self,
                self.event_client,
                "preview-file:add-file",
                lambda data: sync_preview_to_ayon(
                    self, data.get("preview_file_id", ""),
                    data.get("task_id", ""), data.get("project_id", ""),
                ),
                lane="slow",
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
            detail = str(getattr(res, "detail", "") or "")
            logging.error(
                f"Failed to fetch pairing list from {self.entrypoint}/pairing. "
                f"Status: {res.status_code}, Detail: {detail}"
            )
            if pairing_http_error_is_kitsu_login(detail):
                logging.error(
                    "AYON server Kitsu login failed for GET /pairing. The addon uses "
                    "Studio settings secrets (login_email / login_password), not "
                    "this container's KITSU_LOGIN / KITSU_PWD. Update those secrets in "
                    "AYON Studio to match a valid Kitsu user, or ensure local fallback "
                    "can run (processor must be logged into Kitsu already)."
                )
                local = pairing_list_from_processor_session()
                if local:
                    logging.info(
                        "[pairing] Using locally built pairing list (processor Kitsu "
                        "session + GET /api/projects) because server /pairing Kitsu "
                        "login failed."
                    )
                    return local
                raise KitsuSettingsError(
                    "Pairing endpoint failed: Kitsu invalid credentials on the AYON "
                    "server (Studio secrets for the Kitsu addon). "
                    "Local pairing fallback also failed or returned no data."
                )
            if res.status_code == 404:
                logging.error(
                    f"Pairing endpoint returned 404. Addon {self.addon_name}/"
                    f"{self.addon_version} may not be deployed in this bundle."
                )
            else:
                logging.error(
                    f"If this is not a credentials issue, addon "
                    f"{self.addon_name}/{self.addon_version} may be missing from the "
                    "bundle or AYON_ADDON_NAME / AYON_ADDON_VERSION may be wrong."
                )
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
                f"Ensure addon {self.addon_name}/{self.addon_version} is deployed "
                "and Studio Kitsu secrets are valid."
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
        logging.info(
            "Main loop: sync only (kitsu.sync_request). "
            "AYON event thread handles kitsu.comment_update_request."
        )
        startup = True
        loop_count = 0
        last_status_log = time.time()

        while True:
            loop_count += 1

            # Log status every 60 seconds
            current_time = time.time()
            if current_time - last_status_log >= 60:
                logging.info(
                    f"Status: Loop {loop_count}, gazu_thread={self.gazu_listener_thread.is_alive()}, "
                    f"ayon_event_thread={self.ayon_event_thread.is_alive()}"
                )
                last_status_log = current_time

            # Check if event listener thread is still alive
            if not self.gazu_listener_thread.is_alive():
                logging.error(
                    "FATAL: Kitsu event listener thread has died! "
                    "The processor will no longer receive Kitsu events."
                )
                raise RuntimeError("Kitsu event listener thread died")
            if not self.ayon_event_thread.is_alive():
                logging.error(
                    "FATAL: AYON event loop thread has died! "
                    "Comment updates and other AYON events will not be processed."
                )
                raise RuntimeError("AYON event loop thread died")

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

            # Enroll for sync job only (comment_update runs in AYON event thread)
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
