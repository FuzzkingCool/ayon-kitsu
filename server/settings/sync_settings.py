from ayon_server.settings import BaseSettingsModel, SettingsField
from ayon_server.types import NAME_REGEX


#
## Sync users
#
def _roles_enum():
    return [
        {"value": "user", "label": "User"},
        {"value": "manager", "label": "Manager"},
        {"value": "admin", "label": "Admin"},
    ]


class RolesCondition(BaseSettingsModel):
    """Set what Ayon role users should get based in their Kitsu role"""

    admin: str = SettingsField(
        "admin", enum_resolver=_roles_enum, title="Studio manager"
    )
    vendor: str = SettingsField(
        "user", enum_resolver=_roles_enum, title="Vendor"
    )
    client: str = SettingsField(
        "user", enum_resolver=_roles_enum, title="Client"
    )
    manager: str = SettingsField(
        "manager", enum_resolver=_roles_enum, title="Production manager"
    )
    supervisor: str = SettingsField(
        "manager", enum_resolver=_roles_enum, title="Supervisor"
    )
    user: str = SettingsField(
        "user", enum_resolver=_roles_enum, title="Artist"
    )


class SyncUsers(BaseSettingsModel):
    """When a Kitsu user is synced, the default password will be set for the newly created user.
    Please ask the user to change the password inside Ayon.
    """

    enabled: bool = SettingsField(True)
    default_password: str = SettingsField(title="Default Password")
    access_group: str = SettingsField(title="Access Group", regex=NAME_REGEX)
    roles: RolesCondition = SettingsField(
        default_factory=RolesCondition, title="Roles"
    )


#
## Default task info
#
class TaskCondition(BaseSettingsModel):
    _layout: str = "compact"
    name: str = SettingsField("", title="Name")
    short_name: str = SettingsField("", title="Short name")
    icon: str = SettingsField("task_alt", title="Icon", widget="icon")


def _states_enum():
    return [
        {"value": "not_started", "label": "Not started"},
        {"value": "in_progress", "label": "In progress"},
        {"value": "done", "label": "Done"},
        {"value": "blocked", "label": "Blocked"},
    ]


class StatusCondition(BaseSettingsModel):
    _layout: str = "compact"
    short_name: str = SettingsField("", title="Short name")
    state: str = SettingsField(
        "in_progress", enum_resolver=_states_enum, title="State"
    )
    icon: str = SettingsField("task_alt", title="Icon", widget="icon")


class DefaultSyncInfo(BaseSettingsModel):
    """As statuses already have names and short names we only need the short name to match Kitsu with Ayon"""

    default_task_info: list[TaskCondition] = SettingsField(
        default_factory=list, title="Tasks"
    )
    default_status_info: list[StatusCondition] = SettingsField(
        default_factory=list, title="Statuses"
    )


class ChecklistSubtasksSettings(BaseSettingsModel):
    """Map Kitsu pinned comments with checklists to AYON child tasks (processor).

    Index-based keys tie each row to ``{comment_id}:{index}``; reordering rows in
    Kitsu can reassign semantics. Child tasks use the same task type as the
    parent Kitsu-synced task.

    Bulk checklist sync after full project entity sync does not require Content
    sync (no AYON comment activities). Enable **Sync pinned checklists after
    full project sync** for that path."""

    enabled: bool = SettingsField(
        False,
        title="Enable checklist child tasks",
        description=(
            "Create/update AYON child tasks from Kitsu pinned comments with "
            "checklists. For a full-project pass without Content sync, also enable "
            "**Sync pinned checklists after full project sync** below."
        ),
    )
    done_status_name: str = SettingsField(
        "Done",
        title="AYON status name for checked items",
    )
    wip_status_name: str = SettingsField(
        "In Progress",
        title="AYON status name for unchecked items",
    )
    delete_tasks_on_comment_delete: bool = SettingsField(
        True,
        title="Delete child tasks when the Kitsu comment is deleted",
    )
    delete_tasks_when_unpinned: bool = SettingsField(
        False,
        title="Delete child tasks when the comment is unpinned",
    )
    bulk_sync_after_fullsync: bool = SettingsField(
        False,
        title="Sync pinned checklists after full project sync",
        description=(
            "After structural Kitsu→AYON sync for a project, scan Kitsu task "
            "comments and upsert child tasks for pinned checklists. Does not "
            "require Content sync or AYON comment activities."
        ),
    )


class ContentSyncSettings(BaseSettingsModel):
    """Sync Kitsu content (thumbnails, comments, previews) to AYON.
    Disabled by default -- enable after bulk migration via scripts.

    Pinned checklist → AYON child tasks can be synced after full project entity
    sync via **Checklist child tasks** without enabling Content sync here."""

    enabled: bool = SettingsField(False, title="Enable content sync")
    sync_thumbnails: bool = SettingsField(True, title="Sync entity thumbnails")
    sync_comments: bool = SettingsField(
        True, title="Sync task comments to activity feed"
    )
    sync_previews: bool = SettingsField(
        True, title="Sync preview files as reviewables"
    )
    sync_attachments: bool = SettingsField(
        True, title="Sync comment attachments"
    )
    review_product_type: str = SettingsField(
        "review", title="Product type for synced reviews"
    )


class SyncSettings(BaseSettingsModel):
    """Enabling 'Delete projects' will remove projects on Ayon when they get deleted on Kitsu"""

    delete_projects: bool = SettingsField(title="Delete projects")
    sync_users: SyncUsers = SettingsField(
        default_factory=SyncUsers,
        title="Sync users",
    )
    default_sync_info: DefaultSyncInfo = SettingsField(
        default_factory=DefaultSyncInfo,
        title="Default sync info",
    )
    content_sync: ContentSyncSettings = SettingsField(
        default_factory=ContentSyncSettings,
        title="Content sync",
    )
    checklist_subtasks: ChecklistSubtasksSettings = SettingsField(
        default_factory=ChecklistSubtasksSettings,
        title="Checklist child tasks (Kitsu ↔ AYON)",
    )


CONTENT_SYNC_DEFAULT_VALUES = {
    "enabled": False,
    "sync_thumbnails": True,
    "sync_comments": True,
    "sync_previews": True,
    "sync_attachments": True,
    "review_product_type": "review",
}

CHECKLIST_SUBTASKS_DEFAULT_VALUES = {
    "enabled": False,
    "done_status_name": "Done",
    "wip_status_name": "In Progress",
    "delete_tasks_on_comment_delete": True,
    "delete_tasks_when_unpinned": False,
    "bulk_sync_after_fullsync": False,
}

SYNC_DEFAULT_VALUES = {
    "delete_projects": False,
    "content_sync": CONTENT_SYNC_DEFAULT_VALUES,
    "checklist_subtasks": CHECKLIST_SUBTASKS_DEFAULT_VALUES,
    "sync_users": {
        "enabled": False,
        "default_password": "default_password",
        "access_group": "kitsu_group",
    },
    "default_sync_info": {
        "default_task_info": [
            {
                "name": "Concept",
                "short_name": "cncp",
                "icon": "lightbulb",
            },
            {
                "name": "Modeling",
                "short_name": "mdl",
                "icon": "language",
            },
            {
                "name": "Shading",
                "short_name": "shdn",
                "icon": "format_paint",
            },
            {
                "name": "Rigging",
                "short_name": "rig",
                "icon": "construction",
            },
            {
                "name": "Edit",
                "short_name": "edit",
                "icon": "cut",
            },
            {
                "name": "Storyboard",
                "short_name": "stry",
                "icon": "image",
            },
            {
                "name": "Layout",
                "short_name": "lay",
                "icon": "nature_people",
            },
            {
                "name": "Animation",
                "short_name": "anim",
                "icon": "directions_run",
            },
            {
                "name": "Lighting",
                "short_name": "lgt",
                "icon": "highlight",
            },
            {
                "name": "FX",
                "short_name": "fx",
                "icon": "local_fire_department",
            },
            {
                "name": "Compositing",
                "short_name": "comp",
                "icon": "layers",
            },
            {
                "name": "Recording",
                "short_name": "rcrd",
                "icon": "video_camera_back",
            },
        ],
        "default_status_info": [
            {
                "short_name": "todo",
                "state": "not_started",
                "icon": "fiber_new",
            },
            {
                "short_name": "neutral",
                "state": "in_progress",
                "icon": "timer",
            },
            {
                "short_name": "wip",
                "state": "in_progress",
                "icon": "play_arrow",
            },
            {
                "short_name": "wfa",
                "state": "in_progress",
                "icon": "visibility",
            },
            {
                "short_name": "retake",
                "state": "in_progress",
                "icon": "timer",
            },
            {
                "short_name": "done",
                "state": "done",
                "icon": "task_alt",
            },
            {
                "short_name": "ready",
                "state": "not_started",
                "icon": "timer",
            },
            {
                "short_name": "approved",
                "state": "done",
                "icon": "task_alt",
            },
            {
                "short_name": "rejected",
                "state": "blocked",
                "icon": "block",
            },
        ],
    },
}
