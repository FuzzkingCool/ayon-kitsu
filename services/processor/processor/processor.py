import os
import socket
import sys
import threading
import time

import ayon_api
import gazu
from nxtools import log_traceback, logging

from .fullsync import project_full_sync
from .log_handler import ServerLogHandler
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
from .utils import resolve_feedback_status

if service_name := os.environ.get("AYON_SERVICE_NAME"):
    logging.user = service_name

SENDER = f"kitsu-processor-{socket.gethostname()}"


class KitsuServerError(Exception):
    pass


class KitsuSettingsError(Exception):
    pass


class KitsuProcessor:
    def __init__(self):
        #
        # Connect to Ayon
        #
        try:
            ayon_api.init_service()
            connected = True
        except Exception:
            log_traceback()
            connected = False

        if not connected:
            time.sleep(10)
            print("KitsuProcessor failed to connect to Ayon")
            sys.exit(1)

        #
        # Load settings and stuff...
        #

        self.addon_name = ayon_api.get_service_addon_name()
        self.addon_version = ayon_api.get_service_addon_version()
        self.settings = ayon_api.get_service_addon_settings()
        self.entrypoint = f"/addons/{self.addon_name}/{self.addon_version}"

        #
        # Setup server log forwarding (after service is fully initialized)
        #
        self.server_log_handler = None
        try:
            # nxtools.logging might use a custom logger, try both approaches
            import logging as std_logging

            self.server_log_handler = ServerLogHandler(
                sender=SENDER,
                level=std_logging.DEBUG,  # Forward all log levels
                batch_size=10,  # Batch 10 logs before sending
                flush_interval=5.0,  # Flush every 5 seconds
            )
            # Add to standard Python root logger
            std_logging.getLogger().addHandler(self.server_log_handler)
            # Also try to add to nxtools logger if it's different
            try:
                if hasattr(logging, "getLogger"):
                    nxtools_logger = logging.getLogger()
                    if nxtools_logger != std_logging.getLogger():
                        nxtools_logger.addHandler(self.server_log_handler)
            except Exception:
                pass

            # Test the log handler immediately
            logging.info(
                "[ayon-kitsu][processor] Server log forwarding enabled"
            )
            logging.info(
                "[ayon-kitsu][processor] TEST: This log should appear on server if forwarding works"
            )

            # Force immediate flush of test logs
            if self.server_log_handler:
                try:
                    self.server_log_handler.flush()
                except Exception:
                    pass
        except Exception as e:
            # Don't fail initialization if log forwarding setup fails
            logging.error(
                f"[ayon-kitsu][processor] Failed to setup server log forwarding: {e}"
            )
            log_traceback("Server log forwarding setup failed")

        #
        # Get list of projects that have been paired
        #
        self.pairing_list = self.get_pairing_list()

        #
        # Get Kitsu server credentials from settings
        #

        try:
            self.kitsu_server_url = (
                self.settings.get("server").rstrip("/") + "/api"
            )

            email_secret = self.settings.get("login_email")
            password_secret = self.settings.get("login_password")

            if not email_secret:
                raise ValueError(f"Email secret `{email_secret}` not set")

            if not password_secret:
                raise ValueError(
                    f"Password secret `{password_secret}` not set"
                )

            try:
                self.kitsu_login_email = ayon_api.get_secret(email_secret)[
                    "value"
                ]
                self.kitsu_login_password = ayon_api.get_secret(
                    password_secret
                )["value"]
            except KeyError as e:
                raise KitsuSettingsError(f"Secret `{e}` not found") from e

            assert self.kitsu_login_password, "Kitsu password not set"
            assert self.kitsu_server_url, "Kitsu server not set"
            assert self.kitsu_login_email, "Kitsu email not set"
        except AssertionError as e:
            logging.error(f"KitsuProcessor failed to initialize: {e}")
            raise KitsuSettingsError() from e

        #
        # Connect to Kitsu
        #
        gazu.set_host(self.kitsu_server_url)
        if not gazu.client.host_is_valid():
            raise KitsuServerError(
                f"Kitsu server `{self.kitsu_server_url}` is not valid"
            )

        try:
            gazu.log_in(self.kitsu_login_email, self.kitsu_login_password)
            logging.info(f"Gazu logged in as {self.kitsu_login_email}")
        except gazu.exception.AuthFailedException as e:
            raise KitsuServerError(f"Kitsu login failed: {e}") from e

        # init event client
        self.kitsu_events_url = self.kitsu_server_url.replace(
            "api", "socket.io"
        )
        gazu.set_event_host(self.kitsu_events_url)
        self.event_client = gazu.events.init()

        # ============= Add Kitsu Event Listeners ==============
        gazu_listener_thread = threading.Thread(target=self.run_gazu_listeners)
        gazu_listener_thread.start()

    def run_gazu_listeners(self):
        def safe_handler(handler_func, event_type: str):
            """Wrap handler to catch and log errors without crashing the listener thread."""

            def wrapper(data):
                try:
                    handler_func(data)
                except Exception as e:
                    logging.error(
                        f"[gazu_listener] Error handling {event_type} event: {e}"
                    )
                    log_traceback(f"Error in {event_type} handler")

            return wrapper

        gazu.events.add_listener(
            self.event_client,
            "project:update",
            safe_handler(
                lambda data: update_project(self, data), "project:update"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "project:delete",
            safe_handler(
                lambda data: delete_project(self, data), "project:delete"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "asset:new",
            safe_handler(
                lambda data: create_or_update_asset(self, data), "asset:new"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "asset:update",
            safe_handler(
                lambda data: create_or_update_asset(self, data), "asset:update"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "asset:delete",
            safe_handler(
                lambda data: delete_asset(self, data), "asset:delete"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "episode:new",
            safe_handler(
                lambda data: create_or_update_episode(self, data),
                "episode:new",
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "episode:update",
            safe_handler(
                lambda data: create_or_update_episode(self, data),
                "episode:update",
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "episode:delete",
            safe_handler(
                lambda data: delete_episode(self, data), "episode:delete"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "sequence:new",
            safe_handler(
                lambda data: create_or_update_sequence(self, data),
                "sequence:new",
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "sequence:update",
            safe_handler(
                lambda data: create_or_update_sequence(self, data),
                "sequence:update",
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "sequence:delete",
            safe_handler(
                lambda data: delete_sequence(self, data), "sequence:delete"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "shot:new",
            safe_handler(
                lambda data: create_or_update_shot(self, data), "shot:new"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "shot:update",
            safe_handler(
                lambda data: create_or_update_shot(self, data), "shot:update"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "shot:delete",
            safe_handler(lambda data: delete_shot(self, data), "shot:delete"),
        )
        gazu.events.add_listener(
            self.event_client,
            "task:new",
            safe_handler(
                lambda data: create_or_update_task(self, data), "task:new"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "task:update",
            safe_handler(
                lambda data: create_or_update_task(self, data), "task:update"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "task:delete",
            safe_handler(lambda data: delete_task(self, data), "task:delete"),
        )
        gazu.events.add_listener(
            self.event_client,
            "edit:new",
            safe_handler(
                lambda data: create_or_update_edit(self, data), "edit:new"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "edit:update",
            safe_handler(
                lambda data: create_or_update_edit(self, data), "edit:update"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "edit:delete",
            safe_handler(lambda data: delete_edit(self, data), "edit:delete"),
        )
        gazu.events.add_listener(
            self.event_client,
            "person:new",
            safe_handler(
                lambda data: create_or_update_person(self, data), "person:new"
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "person:update",
            safe_handler(
                lambda data: create_or_update_person(self, data),
                "person:update",
            ),
        )
        gazu.events.add_listener(
            self.event_client,
            "person:delete",
            safe_handler(
                lambda data: delete_person(self, data), "person:delete"
            ),
        )
        # Concept events were fixed in Zou 0.19.0, so listen only if
        # the user is running Zou euqual or above 0.19.0
        if tuple(gazu.client.get_api_version().split(".")) >= ("0", "19", "0"):
            gazu.events.add_listener(
                self.event_client,
                "concept:new",
                safe_handler(
                    lambda data: create_or_update_concept(self, data),
                    "concept:new",
                ),
            )
            gazu.events.add_listener(
                self.event_client,
                "concept:update",
                safe_handler(
                    lambda data: create_or_update_concept(self, data),
                    "concept:update",
                ),
            )
            gazu.events.add_listener(
                self.event_client,
                "concept:delete",
                safe_handler(
                    lambda data: delete_concept(self, data), "concept:delete"
                ),
            )
        logging.info("Gazu event listeners added")
        gazu.events.run_client(self.event_client)

    def get_pairing_list(self):
        """maintain a list of pairings so that we can check
        the kitsu change is in a paired project and get the ayon project name
        """
        logging.info("get_pairing_list")
        res = ayon_api.get(f"{self.entrypoint}/pairing")

        assert res.status_code == 200, (
            f"{self.entrypoint}/pairing failed. "
            f" Status code '{res.status_code}': {res.detail}"
        )

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
        logging.info(
            f"[ayon-kitsu][processor] ========== PROCESSOR STARTED =========="
        )
        logging.info(f"[ayon-kitsu][processor] Version: v{self.addon_version}")
        logging.info(
            f"[ayon-kitsu][processor] Paired projects: {len(self.pairing_list)}"
        )
        logging.info(
            f"[ayon-kitsu][processor] Listening for events: kitsu.sync_request, kitsu.comment_update_request"
        )
        logging.info(f"[ayon-kitsu][processor] Sender ID: {SENDER}")
        logging.info(
            f"[ayon-kitsu][processor] Server entrypoint: {self.entrypoint}"
        )
        logging.info(
            f"[ayon-kitsu][processor] ========================================"
        )

        # Send a startup event to verify processor is running
        try:
            ayon_api.dispatch_event(
                topic="addon.kitsu.processor.started",
                sender=SENDER,
                description=f"Kitsu processor v{self.addon_version} started",
                summary={
                    "version": self.addon_version,
                    "paired_projects": len(self.pairing_list),
                    "sender": SENDER,
                },
                finished=True,
                store=True,
            )
            logging.info(
                "[ayon-kitsu][processor] Startup event dispatched to server"
            )
        except Exception as e:
            logging.warning(
                f"[ayon-kitsu][processor] Failed to dispatch startup event: {e}"
            )

        startup = True

        while True:
            processed_job = False

            # Sync all paired projects on startup
            if startup:
                logging.info(
                    f"[ayon-kitsu][processor] Running initial sync for {len(self.pairing_list)} projects"
                )
                for pair in self.pairing_list:
                    project_id = pair.get("kitsuProjectId")
                    project_name = pair.get("ayonProjectName")
                    if project_id and project_name:
                        try:
                            project_full_sync(self, project_id, project_name)
                            logging.info(
                                f"[ayon-kitsu][processor] Synced {project_name}"
                            )
                        except Exception as e:
                            logging.error(
                                f"[ayon-kitsu][processor] Sync failed for {project_name}: {e}"
                            )
                            log_traceback(f"Sync failed for {project_name}")
                    else:
                        logging.warning(
                            f"[ayon-kitsu][processor] Incomplete pair: {pair}"
                        )
                startup = False
                logging.info("[ayon-kitsu][processor] Initial sync complete")

            # Check for a new sync job
            try:
                job = ayon_api.enroll_event_job(
                    source_topic="kitsu.sync_request",
                    target_topic="kitsu.sync",
                    sender=SENDER,
                    description="Syncing Kitsu to Ayon",
                    max_retries=3,
                )
            except Exception as e:
                logging.debug(
                    f"[ayon-kitsu][processor] No sync job available: {e}"
                )
                job = None

            if job:
                processed_job = True
                src_job = ayon_api.get_event(job["dependsOn"])

                kitsu_project_id = src_job["summary"]["kitsuProjectId"]
                ayon_project_name = src_job["project"]

                logging.info(
                    f"[ayon-kitsu][processor] Syncing {ayon_project_name}"
                )

                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="in_progress",
                    project_name=ayon_project_name,
                    description=f"Syncing Kitsu project {ayon_project_name}",
                )

                try:
                    project_full_sync(
                        self, kitsu_project_id, ayon_project_name
                    )
                    self.set_paired_ayon_project(
                        kitsu_project_id, ayon_project_name
                    )
                except Exception as sync_error:
                    log_traceback(
                        f"Unable to sync kitsu project {ayon_project_name}"
                    )
                    ayon_api.update_event(
                        job["id"],
                        sender=SENDER,
                        status="failed",
                        project_name=ayon_project_name,
                        description=f"Sync failed: {str(sync_error)}",
                    )
                else:
                    ayon_api.update_event(
                        job["id"],
                        sender=SENDER,
                        status="finished",
                        project_name=ayon_project_name,
                        description=f"Kitsu sync finished for {ayon_project_name}",
                    )

            # Enroll for comment update requests from server (for uniqueSprites bubble-up)
            # Server handler gathers all data and dispatches kitsu.comment_update_request
            comment_job = None
            try:
                comment_job = ayon_api.enroll_event_job(
                    source_topic="kitsu.comment_update_request",
                    target_topic="kitsu.comment_update",
                    sender=SENDER,
                    description="Update Kitsu comment with uniqueSprites",
                    max_retries=3,
                )
                if comment_job:
                    logging.info(
                        f"[ayon-kitsu][processor] Enrolled for comment update job: {comment_job.get('id')}"
                    )
            except Exception as e:
                # Only log at debug level if no job available (normal case)
                if (
                    "No job available" in str(e)
                    or "not found" in str(e).lower()
                ):
                    logging.debug(
                        f"[ayon-kitsu][processor] No comment update job available: {e}"
                    )
                else:
                    logging.warning(
                        f"[ayon-kitsu][processor] Error enrolling for comment update job: {e}"
                    )
                comment_job = None

            if comment_job:
                processed_job = True
                logging.info(
                    f"[ayon-kitsu][processor] Processing comment update job: {comment_job.get('id')}"
                )
                self._handle_comment_update_job(comment_job)

            if not processed_job:
                # Heartbeat every 5 minutes when idle
                current_time = time.time()
                if not hasattr(self, "_last_heartbeat"):
                    self._last_heartbeat = current_time
                    self._heartbeat_count = 0

                if current_time - self._last_heartbeat > 300:  # 5 minutes
                    self._heartbeat_count += 1
                    logging.debug(
                        f"[ayon-kitsu][processor] Heartbeat #{self._heartbeat_count} - waiting for jobs"
                    )
                    self._last_heartbeat = current_time

                    # Dispatch heartbeat event for monitoring
                    try:
                        ayon_api.dispatch(
                            "addon.kitsu.processor.heartbeat",
                            sender=SENDER,
                            description=f"Processor heartbeat #{self._heartbeat_count}",
                            summary={
                                "status": "alive",
                                "heartbeat_count": self._heartbeat_count,
                                "addon_version": self.addon_version,
                                "paired_projects": len(self.pairing_list),
                            },
                        )
                    except Exception as e:
                        logging.debug(
                            f"[ayon-kitsu][processor] Heartbeat dispatch failed: {e}"
                        )

                time.sleep(5)

        logging.info("[ayon-kitsu][processor] Processor finished - exiting")
        gazu.log_out()

    def _handle_comment_update_job(self, job: dict):
        """Handle a comment update request from server for uniqueSprites bubble-up.

        Server handler has already gathered all data and validated settings.
        This handler only needs to perform Kitsu operations using gazu.
        """
        try:
            logging.info(
                f"[ayon-kitsu][processor] Handling comment update job {job.get('id')}"
            )
            src_event = ayon_api.get_event(job["dependsOn"])
            project_name = src_event.get("project")
            summary = src_event.get("summary", {})
            payload = src_event.get("payload", {})

            logging.info(
                f"[ayon-kitsu][processor] Comment update event data - "
                f"project: {project_name}, task_id: {summary.get('task_id')}, "
                f"unique_sprites: {summary.get('unique_sprites')}"
            )

            logging.info(
                f"[ayon-kitsu][processor] Processing comment update request for "
                f"task {summary.get('task_id')} (project: {project_name})"
            )

            ayon_api.update_event(
                job["id"],
                sender=SENDER,
                status="in_progress",
                project_name=project_name,
                description="Updating Kitsu comment with uniqueSprites",
            )

            # All data is provided by server handler in event summary/payload
            comment_payload = {
                "task_id": summary.get("task_id"),
                "kitsu_task_id": summary.get("kitsu_task_id"),
                "product_name": summary.get("product_name"),
                "task_name": summary.get("task_name"),
                "unique_sprites": summary.get("unique_sprites"),
                "new_status": summary.get("new_status"),
                "old_status": summary.get("old_status"),
                "version": summary.get("version", 1),
                "template_cfg": payload.get("template_cfg", {}),
            }

            if not comment_payload.get("kitsu_task_id"):
                logging.warning(
                    f"[ayon-kitsu][processor] Missing kitsu_task_id in event summary"
                )
                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="finished",
                    description="Missing kitsu_task_id",
                )
                return

            # Process the comment update
            try:
                self.update_kitsu_comment(comment_payload, project_name)
                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="finished",
                    description=f"Updated Kitsu with uniqueSprites={comment_payload.get('unique_sprites')}",
                )
                logging.info(
                    f"[ayon-kitsu][processor] Successfully updated Kitsu for task "
                    f"{comment_payload.get('task_id')}"
                )
            except Exception as e:
                logging.error(
                    f"[ayon-kitsu][processor] Comment update failed: {e}"
                )
                log_traceback("Comment update failed")
                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="failed",
                    description=f"Failed: {str(e)}",
                )

        except Exception as e:
            logging.error(
                f"[ayon-kitsu][processor] Comment update handling failed: {e}"
            )
            log_traceback("Comment update handling failed")
            try:
                ayon_api.update_event(
                    job["id"],
                    sender=SENDER,
                    status="failed",
                    description=f"Error: {str(e)}",
                )
            except Exception:
                pass

    def update_kitsu_comment(self, payload: dict, project_name: str | None):
        """Update Kitsu task comment using payload from comment update event.

        All data is provided by server handler - no AYON API calls needed.
        """
        if not project_name:
            raise RuntimeError("Missing project name for Kitsu comment update")

        kitsu_task_id = payload.get("kitsu_task_id")
        product_name = payload.get("product_name")
        task_name = payload.get("task_name")
        unique_sprites = payload.get("unique_sprites")
        template_cfg = payload.get("template_cfg", {})

        if not kitsu_task_id:
            raise RuntimeError(
                "Missing kitsu_task_id in Kitsu comment payload"
            )

        kitsu_task = gazu.task.get_task(kitsu_task_id)
        if not kitsu_task:
            raise RuntimeError(f"Kitsu task {kitsu_task_id} not found")

        if unique_sprites is not None:
            logging.info(
                f"[ayon-kitsu][processor] Bubbling up uniqueSprites={unique_sprites} "
                f"to parent Asset for task '{task_name}' (Kitsu task ID: {kitsu_task_id})"
            )
            try:
                self._bubble_up_unique_sprites_to_parent(
                    kitsu_task, unique_sprites
                )
                logging.info(
                    f"[ayon-kitsu][processor] Successfully bubbled up uniqueSprites={unique_sprites}"
                )
            except Exception as e:
                logging.error(
                    f"[ayon-kitsu][processor] Failed to bubble up uniqueSprites: {e}"
                )
                log_traceback("Bubble-up failed")
                raise
        else:
            logging.debug(
                "[ayon-kitsu][processor] No uniqueSprites to bubble up"
            )

        # Use template config from event payload (provided by server)
        from .utils import render_kitsu_comment

        data_map = {
            "comment": f"Bubble-up: Status changed to {payload.get('new_status', 'unknown')}",
            "version": payload.get("version", 1),
            "family": "review",
            "name": product_name or task_name or "Review",
        }
        if unique_sprites is not None:
            data_map["uniqueSprites"] = str(unique_sprites)

        comment = render_kitsu_comment(template_cfg, data_map)
        if not comment:
            if unique_sprites is not None:
                comment = f"uniqueSprites: {unique_sprites}"
            else:
                raise RuntimeError(
                    "Generated empty Kitsu comment and no uniqueSprites to report"
                )

        current_user = gazu.client.get_current_user()
        note_status = resolve_feedback_status(kitsu_task)

        logging.debug(
            f"[ayon-kitsu][processor] Adding comment to Kitsu task {kitsu_task_id}"
        )

        kitsu_comment = gazu.task.add_comment(
            kitsu_task,
            note_status,
            comment=comment,
            person=current_user,
        )

        comment_id = kitsu_comment.get("id") if kitsu_comment else "No ID"
        logging.info(
            f"[ayon-kitsu][processor] Added Kitsu comment: {comment_id}"
        )

    def _bubble_up_unique_sprites_to_parent(
        self, kitsu_task: dict, unique_sprites: int | str
    ) -> None:
        """Update parent Kitsu Asset with uniqueSprites in extra data."""
        entity_id = kitsu_task.get("entity_id")
        if not entity_id:
            raise RuntimeError(
                f"Kitsu task {kitsu_task.get('id')} has no entity_id"
            )

        entity = gazu.entity.get_entity(entity_id)
        if not entity:
            raise RuntimeError(f"Kitsu entity {entity_id} not found")

        entity_type = entity.get("type")
        entity_name = entity.get("name", "Unknown")

        if entity_type != "Asset":
            logging.debug(
                f"[ayon-kitsu][processor] Skipping bubble-up for non-Asset: {entity_type}"
            )
            return

        logging.debug(
            f"[ayon-kitsu][processor] Updating Asset {entity_name} with uniqueSprites={unique_sprites}"
        )

        updated_asset = gazu.asset.update_asset_data(
            entity, data={"uniqueSprites": unique_sprites}
        )

        result_sprites = updated_asset.get("data", {}).get("uniqueSprites")
        logging.info(
            f"[ayon-kitsu][processor] Updated Kitsu Asset '{entity_name}': uniqueSprites={result_sprites}"
        )
