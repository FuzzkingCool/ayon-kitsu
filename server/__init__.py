from typing import Type

from ayon_server.addons import BaseServerAddon
from ayon_server.api.dependencies import CurrentUser
from ayon_server.api.responses import EmptyResponse
from ayon_server.events import EventModel
from ayon_server.exceptions import ForbiddenException, InvalidSettingsException
from ayon_server.secrets import Secrets
from nxtools import logging

from .kitsu import Kitsu, KitsuMock
from .kitsu.init_pairing import InitPairingRequest, init_pairing, sync_request
from .kitsu.pairing_list import PairingItemModel, get_pairing_list
from .kitsu.sync_issues import dedupe_sync_issue_rows, issue_row_to_dict
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
        self.add_endpoint("/processor/sync-issues", self.processor_sync_issues, method="GET")
        self.add_endpoint("/event-handler/status", self.event_handler_status, method="GET")

        from .event_subscribe import register_event_subscriptions
        register_event_subscriptions(self)

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
        """Check processor service status by looking for recent events.

        This endpoint helps diagnose if the processor service is running and
        processing events.
        """
        from ayon_server.lib.postgres import Postgres

        # Check for recent processor events (job completions, etc.)
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

        return {
            "pending_sync_jobs": pending_sync,
            "pending_comment_jobs": pending_comment,
            "recent_events": recent_events,
            "addon_version": getattr(self, 'version', 'unknown'),
        }

    async def processor_sync_issues(
        self,
        user: CurrentUser,
        project: str | None = None,
        limit: int = 100,
        dedupe: bool = True,
    ) -> dict:
        """Recent Kitsu processor sync failures and partial-sync summaries (stored events)."""
        if not user.is_manager:
            raise ForbiddenException("Only managers can view Kitsu sync issues")

        from ayon_server.lib.postgres import Postgres

        lim = max(1, min(int(limit), 500))

        base_where = """
            (topic ILIKE '%kitsuProcessorSyncEntityFailed%'
             OR topic ILIKE '%kitsuProcessorSyncSummary%')
        """

        if project:
            query = f"""
                SELECT
                    id,
                    topic,
                    description,
                    project AS project_name,
                    status,
                    summary,
                    payload,
                    created_at,
                    updated_at
                FROM events
                WHERE {base_where}
                AND project = $1
                ORDER BY created_at DESC
                LIMIT $2
            """
            args: tuple = (project, lim)
        else:
            query = f"""
                SELECT
                    id,
                    topic,
                    description,
                    project AS project_name,
                    status,
                    summary,
                    payload,
                    created_at,
                    updated_at
                FROM events
                WHERE {base_where}
                ORDER BY created_at DESC
                LIMIT $1
            """
            args = (lim,)

        raw_rows: list[dict] = []
        async for row in Postgres.iterate(query, *args):
            raw_rows.append(issue_row_to_dict(row))

        if dedupe:
            raw_rows = dedupe_sync_issue_rows(raw_rows)

        return {
            "issues": raw_rows,
            "count": len(raw_rows),
            "limit": lim,
            "dedupe": dedupe,
            "addon_version": getattr(self, "version", "unknown"),
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
        if not await self.ensure_kitsu_or_none(mock):
            return []
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
    async def ensure_kitsu_or_none(self, mock: bool = False) -> bool:
        """Ensure Kitsu client is initialized. Return False if settings are not configured (caller may return [])."""
        if self.kitsu is not None:
            return True

        if mock is True:
            self.kitsu = KitsuMock()
            return True

        settings = await self.get_studio_settings()
        if not settings.server:
            return False

        actual_email = await Secrets.get(settings.login_email)
        actual_password = await Secrets.get(settings.login_password)

        if not actual_email or not actual_password:
            return False

        self.kitsu = Kitsu(settings.server, actual_email, actual_password)
        return True

    async def ensure_kitsu(self, mock: bool = False):
        """Ensure Kitsu client is initialized; raise if settings are not configured."""
        if await self.ensure_kitsu_or_none(mock):
            return
        settings = await self.get_studio_settings()
        if not settings.server:
            raise InvalidSettingsException("Kitsu server is not set")
        raise InvalidSettingsException(
            "Kitsu email or password secret is not set"
        )

    #
    # Event handlers (canonical AYON pattern)
    #

    async def on_task_status_changed(self, event: EventModel):
        """Handle task status changes (from push/sync) to bubble up uniqueSprites."""
        from .kitsu.version_status_handler import handle_task_status_change
        await handle_task_status_change(self, event)

    async def on_task_data_changed(self, event: EventModel):
        """Handle task data changes (e.g. UI status change) to bubble up uniqueSprites."""
        from .kitsu.version_status_handler import handle_task_status_change
        await handle_task_status_change(self, event)

    async def event_handler_status(self) -> dict:
        """Check if event handler is registered and working."""
        return {
            "event_handler": "on_task_status_changed / on_task_data_changed",
            "handler_module": "server.kitsu.version_status_handler",
            "handled_topics": [
                "entity.task.status_changed",
                "entity.task.data_changed",
            ],
            "note": "status_changed = push/sync; data_changed = UI and other core updates",
        }
