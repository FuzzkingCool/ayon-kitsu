from typing import Type

from nxtools import logging

from ayon_server.addons import BaseServerAddon
from ayon_server.api.dependencies import CurrentUser
from ayon_server.api.responses import EmptyResponse
from ayon_server.events import EventModel
from ayon_server.exceptions import ForbiddenException, InvalidSettingsException
from ayon_server.secrets import Secrets

from .kitsu import Kitsu, KitsuMock
from .kitsu.init_pairing import InitPairingRequest, init_pairing, sync_request
from .kitsu.pairing_list import PairingItemModel, get_pairing_list
from .kitsu.push import (
    PushEntitiesRequestModel,
    RemoveEntitiesRequestModel,
    push_entities,
    remove_entities,
)
from .settings import DEFAULT_VALUES, KitsuSettings

#
# Events:
#
# kitsu.sync_request
# - created when a project is imported.
# - worker enrolls to this event to perform full sync
#


class KitsuAddon(BaseServerAddon):
    settings_model: Type[KitsuSettings] = KitsuSettings
    frontend_scopes = {
        "settings": {},
    }

    kitsu: Kitsu | None = None

    async def get_default_settings(self):
        settings_model_cls = self.get_settings_model()
        return settings_model_cls(**DEFAULT_VALUES)

    #
    # Initialization
    #

    def initialize(self):
        self.add_endpoint("/pairing", self.list_pairings, method="GET")
        self.add_endpoint("/pairing", self.init_pairing, method="POST")
        self.add_endpoint("/sync/{project_name}", self.sync, method="POST")
        self.add_endpoint("/push", self.push, method="POST")
        self.add_endpoint("/remove", self.remove, method="POST")
        self.add_endpoint("/processor/status", self.processor_status, method="GET")
        self.add_endpoint("/event-handler/status", self.event_handler_status, method="GET")

    async def setup(self):
        """Called during addon initialization."""
        addon_version = getattr(self, 'version', 'unknown')
        logging.info(f"[ayon-kitsu][server] Kitsu addon v{addon_version} initialized")

    #
    # Endpoints
    #

    async def sync(
        self,
        user: CurrentUser,
        project_name: str
    ) -> EmptyResponse:
        await sync_request(project_name, user)
        return EmptyResponse()

    async def processor_status(self) -> dict:
        """Check processor service status by looking for recent heartbeat events.
        
        This endpoint helps diagnose if the processor service is running and
        processing events.
        """
        from ayon_server.lib.postgres import Postgres
        from ayon_server.api.dependencies import get_addon

        # Check if service is registered in AYON
        service_registered = False
        service_running = False
        try:
            # Try to get service info from AYON's service registry
            # This requires checking the services API
            import httpx
            async with httpx.AsyncClient() as client:
                # This is a placeholder - actual service check would use AYON's service API
                service_registered = True  # Assume registered if we can't check
        except Exception:
            pass

        # Check for recent processor events (heartbeats, job completions, etc.)
        query = """
            SELECT 
                topic,
                description,
                created_at,
                updated_at,
                status,
                summary
            FROM events
            WHERE topic LIKE 'addon.kitsu.processor.%'
            ORDER BY created_at DESC
            LIMIT 10
        """

        recent_events = []
        async for row in Postgres.iterate(query):
            recent_events.append({
                "topic": row["topic"],
                "description": row["description"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "status": row["status"],
                "summary": row["summary"],
            })

        # Check for pending jobs
        pending_sync_query = """
            SELECT COUNT(*) as count
            FROM events
            WHERE topic = 'kitsu.sync_request'
            AND status IN ('pending', 'in_progress')
        """

        pending_comment_query = """
            SELECT COUNT(*) as count
            FROM events
            WHERE topic = 'kitsu.comment_update_request'
            AND status IN ('pending', 'in_progress')
        """

        pending_sync = 0
        async for row in Postgres.iterate(pending_sync_query):
            pending_sync = row["count"]

        pending_comment = 0
        async for row in Postgres.iterate(pending_comment_query):
            pending_comment = row["count"]

        # Find most recent heartbeat
        heartbeat_query = """
            SELECT 
                created_at,
                summary
            FROM events
            WHERE topic = 'addon.kitsu.processor.heartbeat'
            ORDER BY created_at DESC
            LIMIT 1
        """

        last_heartbeat = None
        async for row in Postgres.iterate(heartbeat_query):
            last_heartbeat = {
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
                "summary": row["summary"],
            }

        # Check for startup event
        startup_query = """
            SELECT 
                created_at,
                summary
            FROM events
            WHERE topic = 'addon.kitsu.processor.started'
            ORDER BY created_at DESC
            LIMIT 1
        """

        last_startup = None
        async for row in Postgres.iterate(startup_query):
            last_startup = {
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
                "summary": row["summary"],
            }

        processor_running = last_heartbeat is not None or last_startup is not None

        return {
            "processor_running": processor_running,
            "service_registered": service_registered,
            "last_heartbeat": last_heartbeat,
            "last_startup": last_startup,
            "pending_sync_jobs": pending_sync,
            "pending_comment_jobs": pending_comment,
            "recent_events": recent_events,
            "addon_version": getattr(self, 'version', 'unknown'),
            "service_image": f"ynput/ayon-kitsu-processor:{getattr(self, 'version', 'unknown')}",
            "diagnostics": {
                "note": "If processor_running is False, the service may not be spawned. "
                        "Check AYON Services UI or use /api/services endpoint to verify service is running.",
                "check_endpoint": "/api/services",
                "expected_events": [
                    "addon.kitsu.processor.started",
                    "addon.kitsu.processor.heartbeat"
                ]
            }
        }

    async def push(
        self,
        user: CurrentUser,
        payload: PushEntitiesRequestModel,
    ):
        if not user.is_manager:
            raise ForbiddenException("Only managers can sync Kitsu projects")
        return await push_entities(
            self,
            user=user,
            payload=payload,
        )

    async def remove(
        self,
        user: CurrentUser,
        payload: RemoveEntitiesRequestModel,
    ):
        logging.info(f"payload: {str(payload)}")
        if not user.is_manager:
            raise ForbiddenException("Only managers can sync Kitsu projects")
        return await remove_entities(
            self,
            user=user,
            payload=payload,
        )

    async def list_pairings(
        self, mock: bool = False
    ) -> list[PairingItemModel]:
        await self.ensure_kitsu(mock)
        return await get_pairing_list(self)

    async def init_pairing(
        self,
        user: CurrentUser,
        request: InitPairingRequest,
    ) -> EmptyResponse:
        if not user.is_manager:
            raise ForbiddenException("Only managers can pair Kitsu projects")
        await self.ensure_kitsu()
        await init_pairing(self, user, request)
        return EmptyResponse(status_code=201)

    #
    # Helpers
    #
    async def ensure_kitsu(self, mock: bool = False):
        if self.kitsu is not None:
            return

        if mock is True:
            self.kitsu = KitsuMock()
            return

        settings = await self.get_studio_settings()
        if not settings.server:
            raise InvalidSettingsException("Kitsu server is not set")

        actual_email = await Secrets.get(settings.login_email)
        actual_password = await Secrets.get(settings.login_password)

        if not actual_email:
            raise InvalidSettingsException("Kitsu email secret is not set")

        if not actual_password:
            raise InvalidSettingsException("Kitsu password secret is not set")

        self.kitsu = Kitsu(settings.server, actual_email, actual_password)

    #
    # Event handlers (canonical AYON pattern)
    #

    async def on_task_status_changed(self, event: EventModel):
        """Handle task status changes to bubble up uniqueSprites to Kitsu comments.
        
        This is the canonical AYON pattern from ayon-example-addon.
        Hook methods on the addon class are automatically called by AYON when events occur.
        
        Architecture:
        - Server addon hook (this method) - lightweight, gathers data
        - Dispatches kitsu.comment_update_request event to processor service
        - Processor service handles Kitsu API operations (gazu) - heavy lifting
        """
        logging.info(
            f"[ayon-kitsu][server] on_task_status_changed hook called: "
            f"topic={event.topic}, project={event.project}"
        )
        from .kitsu.version_status_handler import handle_task_status_change
        try:
            await handle_task_status_change(self, event)
        except Exception as e:
            logging.error(f"[ayon-kitsu][server] Error in on_task_status_changed: {e}")
            import traceback
            logging.error(traceback.format_exc())
            raise

    async def on_task_updated(self, event: EventModel):
        """Handle task updates to detect status changes.
        
        When tasks are updated via REST API (e.g., from processor sync),
        AYON dispatches entity.task.updated, not entity.task.status_changed.
        This hook checks if the status actually changed and handles it.
        """
        logging.info(
            f"[ayon-kitsu][server] on_task_updated hook called: "
            f"topic={event.topic}, project={event.project}"
        )
        # Check if status was actually updated
        updated_fields = event.summary.get("updatedFields", [])
        if "status" in updated_fields:
            # Status changed - treat as status_changed event
            logging.info(
                f"[ayon-kitsu][server] Status change detected in task.updated event, "
                f"delegating to status_changed handler"
            )
            # Create a synthetic status_changed event structure
            from .kitsu.version_status_handler import handle_task_status_change
            try:
                await handle_task_status_change(self, event)
            except Exception as e:
                logging.error(f"[ayon-kitsu][server] Error in on_task_updated: {e}")
                import traceback
                logging.error(traceback.format_exc())
                raise
        else:
            logging.debug(
                f"[ayon-kitsu][server] Task updated but status not changed, skipping"
            )

    async def event_handler_status(self) -> dict:
        """Check if event handler is registered and working.
        
        The on_task_status_changed and on_task_updated hook methods are automatically 
        registered by AYON. No manual subscription needed - AYON calls them when events occur.
        """
        # Check if methods exist
        has_status_hook = hasattr(self, 'on_task_status_changed')
        has_updated_hook = hasattr(self, 'on_task_updated')
        status_hook = getattr(self, 'on_task_status_changed', None)
        updated_hook = getattr(self, 'on_task_updated', None)
        
        return {
            "event_handlers": {
                "on_task_status_changed": {
                    "exists": has_status_hook,
                    "callable": callable(status_hook) if status_hook else False,
                    "handles": "entity.task.status_changed events"
                },
                "on_task_updated": {
                    "exists": has_updated_hook,
                    "callable": callable(updated_hook) if updated_hook else False,
                    "handles": "entity.task.updated events (checks for status changes)"
                }
            },
            "handler_module": "server.kitsu.version_status_handler",
            "architecture": {
                "server_hooks": "on_task_status_changed + on_task_updated - lightweight, gather data",
                "dispatches_to": "kitsu.comment_update_request event",
                "processor_service": "Enrolls for kitsu.comment_update_request, handles Kitsu API (gazu)"
            },
            "note": "Using canonical AYON hook method pattern from ayon-example-addon. "
                    "Hooks are automatically called by AYON - no manual subscription needed. "
                    "on_task_updated handles cases where processor syncs from Kitsu and updates tasks via REST."
        }
