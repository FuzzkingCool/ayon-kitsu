import json
import time
from typing import TYPE_CHECKING, Any, Literal, get_args

import httpx
from nxtools import logging, slugify

from ayon_server.auth.session import Session
from ayon_server.entities import FolderEntity, ProjectEntity, TaskEntity, UserEntity
from ayon_server.events import dispatch_event
from ayon_server.helpers.deploy_project import anatomy_to_project_data
from ayon_server.lib.postgres import Postgres
from ayon_server.types import Field, OPModel

from .anatomy import get_kitsu_project_anatomy, parse_attrib
from .constants import (
    CONSTANT_KITSU_MODELS,
)
from .utils import (
    allocate_unique_concept_folder_name_label,
    calculate_end_frame,
    create_folder,
    create_task,
    delete_folder,
    delete_task,
    find_task_id_by_folder_name_type,
    get_folder_by_kitsu_id,
    get_task_by_kitsu_id,
    get_user_by_kitsu_id,
    is_folder_parent_name_unique_violation,
    is_task_folder_name_unique_violation,
    update_project,

    update_folder,
    update_task,
)


from .addon_helpers import to_username, required_values
from .concept_utils import (
    concept_entity_model_is_per_linked_entity,
    concept_folder_base_slug,
    concept_folder_display_name,
    concept_primary_title_for_folder,
    concept_vizdev_surrogate_for_linked_entity,
    concept_vizdev_surrogate_kitsu_id,
    concept_vizdev_surrogate_unlinked_pool,
    normalize_entity_concept_links,
)
from .playlist_entity_sync import delete_playlist as delete_playlist_entity
from .playlist_entity_sync import sync_playlist as sync_playlist_entity

if TYPE_CHECKING:
    from .. import KitsuAddon


EntityDict = dict[str, Any]


async def _kitsu_fetch_linked_entity_names(
    addon: "KitsuAddon",
    link_ids: Any,
) -> list[str]:
    """GET each linked entity's ``name`` (same order as ``entity_concept_links``)."""
    if not link_ids or not isinstance(link_ids, (list, tuple)):
        return []
    kitsu = addon.kitsu
    if kitsu is None:
        return []
    out: list[str] = []
    for lid in link_ids:
        eid = str(lid).strip() if lid is not None else ""
        if not eid:
            continue
        try:
            resp = await kitsu.get(f"data/entities/{eid}")
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logging.debug(
                "[concept_push] GET data/entities/%s failed (linked concept title)",
                eid,
                exc_info=True,
            )
            continue
        if not isinstance(data, dict):
            continue
        nm = (data.get("name") or "").strip()
        if nm:
            out.append(nm)
    return out


KitsuEntityType = Literal[
    "Asset",
    "Shot",
    "Sequence",
    "Episode",
    "Edit",
    "Concept",
    "Task",
    "Person",
    "Project",
    "Playlist",
]


class PushEntitiesRequestModel(OPModel):
    project_name: str
    entities: list[EntityDict] = Field(..., title="List of entities to sync")
    mock: bool | None = None  # optional param for tests


class RemoveEntitiesRequestModel(OPModel):
    project_name: str
    entities: list[EntityDict] = Field(..., title="List of entities to remove")


async def get_root_folder_id(
    user: "UserEntity",
    project_name: str,
    kitsu_type: KitsuEntityType,
    kitsu_type_id: str,
    subfolder_id: str | None = None,
    subfolder_name: str | None = None,
) -> str:
    """
    Get the root folder ID for a given Kitsu type and ID.
    If a folder/subfolder does not exist, it will be created.
    """
    res = await Postgres.fetch(
        f"""
        SELECT id FROM project_{project_name}.folders
        WHERE data->>'kitsuId' = $1
        """,
        kitsu_type_id,
    )

    if res:
        id = res[0]["id"]
    else:
        folder = await create_folder(
            project_name=project_name,
            name=kitsu_type,
            data={"kitsuId": kitsu_type_id},
        )
        id = folder.id

    if not (subfolder_id or subfolder_name):
        return id

    res = await Postgres.fetch(
        f"""
        SELECT id FROM project_{project_name}.folders
        WHERE data->>'kitsuId' = $1
        """,
        subfolder_id,
    )

    if res:
        sub_id = res[0]["id"]
    else:
        sub_folder = await create_folder(
            project_name=project_name,
            name=subfolder_name,
            parent_id=id,
            data={"kitsuId": subfolder_id},
        )
        sub_id = sub_folder.id
    return sub_id


def _concept_relink_folder_name_slugs(
    entity_dict: dict[str, Any],
    *,
    sanitize_folder_display: bool,
) -> list[str]:
    """Unique slug candidates for matching orphan Concept folders (legacy naming)."""
    out: list[str] = []

    def _add_slugs_from_raw(raw: str) -> None:
        stem = (raw or "").strip() or "folder"
        legacy_slug = slugify(stem, separator="_")
        display_stem = concept_folder_display_name(
            stem,
            sanitize=sanitize_folder_display,
        )
        display_slug = slugify(display_stem, separator="_")
        for s in (legacy_slug, display_slug):
            if s and s not in out:
                out.append(s)

    primary = (concept_primary_title_for_folder(entity_dict) or "").strip() or "folder"
    disp = concept_folder_display_name(
        primary,
        sanitize=sanitize_folder_display,
    )
    bslug = concept_folder_base_slug(disp)
    for n in range(1, 20):
        cand = bslug if n == 1 else f"{bslug}_{n}"
        if cand and cand not in out:
            out.append(cand)

    _add_slugs_from_raw(primary)

    name = (entity_dict.get("name") or "").strip()
    code = (entity_dict.get("code") or "").strip()
    if name and code and name != code:
        alt = code if primary == name else name
        _add_slugs_from_raw(alt)

    return out


async def try_relink_orphan_concept_folder(
    user: "UserEntity",
    project: "ProjectEntity",
    entity_dict: "EntityDict",
    existing_folders: dict[str, Any],
    *,
    sanitize_folder_display: bool = True,
) -> bool:
    """Set data.kitsuId on a legacy Concept folder (slug from raw Kitsu name).

    Used when an AYON folder was created without ``data.kitsuId`` so
    ``get_folder_by_kitsu_id`` misses and the folder keeps an image-style name.
    Only Concepts with ``parent_id is None`` (under the Concepts root) are handled.
    """
    if entity_dict.get("parent_id") is not None:
        return False

    name_candidates = _concept_relink_folder_name_slugs(
        entity_dict,
        sanitize_folder_display=sanitize_folder_display,
    )

    concepts_root_id = await get_root_folder_id(
        user=user,
        project_name=project.name,
        kitsu_type="Concepts",
        kitsu_type_id="concept",
    )

    res = await Postgres.fetch(
        f"""
        SELECT id, data FROM project_{project.name}.folders
        WHERE parent_id = $1
          AND folder_type = 'Concept'
          AND name = ANY($2::text[])
          AND (
            data IS NULL
            OR data->>'kitsuId' IS NULL
            OR data->>'kitsuId' = ''
          )
        """,
        concepts_root_id,
        name_candidates,
    )

    if not res:
        return False
    if len(res) > 1:
        logging.warning(
            "[concept_relink] ambiguous: %s Concept folders under Concepts root "
            "matching %r, skipping",
            len(res),
            name_candidates,
        )
        return False

    folder_id = res[0]["id"]
    folder = await FolderEntity.load(project.name, folder_id)
    merged = {**(folder.data or {}), "kitsuId": entity_dict["id"]}
    folder.data = merged
    await folder.save()
    existing_folders[entity_dict["id"]] = folder.id
    logging.info(
        "[concept_relink] set kitsuId on folder %r (name=%r) -> concept %s",
        folder_id,
        folder.name,
        entity_dict["id"],
    )
    event = {
        "topic": "entity.folder.updated",
        "description": f"Folder {folder.name} updated",
        "summary": {"entityId": folder.id, "parentId": folder.parent_id},
        "project": project.name,
    }
    await dispatch_event(**event)
    return True


async def try_relink_orphan_concept_subfolder(
    _user: "UserEntity",
    project: "ProjectEntity",
    entity_dict: "EntityDict",
    existing_folders: dict[str, Any],
    *,
    sanitize_folder_display: bool = True,
) -> bool:
    """Set ``data.kitsuId`` on a nested Concept folder (parent is not the Concepts root).

    Without this, a folder with the right slug but missing ``kitsuId`` is invisible
    to ``get_folder_by_kitsu_id``, and create_folder collides (409) on re-sync.
    """
    parent_kitsu = entity_dict.get("parent_id")
    if not parent_kitsu:
        return False
    name_candidates = _concept_relink_folder_name_slugs(
        entity_dict,
        sanitize_folder_display=sanitize_folder_display,
    )
    if not name_candidates:
        return False

    parent_ayon = await get_folder_by_kitsu_id(
        project.name,
        str(parent_kitsu),
        existing_folders,
    )
    if parent_ayon is None:
        return False
    parent_ayon_id = parent_ayon.id

    res = await Postgres.fetch(
        f"""
        SELECT id, data FROM project_{project.name}.folders
        WHERE parent_id = $1
          AND folder_type = 'Concept'
          AND name = ANY($2::text[])
          AND (
            data IS NULL
            OR data->>'kitsuId' IS NULL
            OR data->>'kitsuId' = ''
          )
        """,
        parent_ayon_id,
        name_candidates,
    )
    if not res:
        return False
    if len(res) > 1:
        logging.warning(
            "[concept_relink] ambiguous subfolder: %s Concept folders under %s "
            "matching %r, skipping",
            len(res),
            parent_ayon_id[:8],
            name_candidates,
        )
        return False

    folder_id = res[0]["id"]
    folder = await FolderEntity.load(project.name, folder_id)
    merged = {**(folder.data or {}), "kitsuId": entity_dict["id"]}
    folder.data = merged
    await folder.save()
    existing_folders[entity_dict["id"]] = folder.id
    logging.info(
        "[concept_relink] set kitsuId on subfolder %r (name=%r) -> concept %s",
        folder_id,
        folder.name,
        entity_dict["id"],
    )
    event = {
        "topic": "entity.folder.updated",
        "description": f"Folder {folder.name} updated",
        "summary": {"entityId": folder.id, "parentId": folder.parent_id},
        "project": project.name,
    }
    await dispatch_event(**event)
    return True


async def try_migrate_concept_folder_from_legacy_concept_ids(
    project: "ProjectEntity",
    *,
    parent_ayon_folder_id: str,
    new_kitsu_id: str,
    source_concept_ids: list[str],
    existing_folders: dict[str, Any],
) -> bool:
    """Point ``data.kitsuId`` from a legacy Kitsu **concept** id to a linked entity id."""
    ids = [str(x).strip() for x in source_concept_ids if str(x).strip()]
    if not ids or not str(new_kitsu_id).strip():
        return False
    res = await Postgres.fetch(
        f"""
        SELECT id, data FROM project_{project.name}.folders
        WHERE parent_id = $1
          AND folder_type = 'Concept'
          AND data->>'kitsuId' = ANY($2::text[])
        LIMIT 5
        """,
        parent_ayon_folder_id,
        ids,
    )
    if len(res) != 1:
        if len(res) > 1:
            logging.warning(
                "[concept_migrate] ambiguous legacy Concept folders for kitsuId=%s "
                "parent=%s (matches=%s)",
                str(new_kitsu_id)[:8],
                str(parent_ayon_folder_id)[:8],
                len(res),
            )
        return False
    folder_id = res[0]["id"]
    folder = await FolderEntity.load(project.name, folder_id)
    merged_data = dict(folder.data or {})
    merged_data["kitsuId"] = str(new_kitsu_id)
    prev_src = merged_data.get("kitsuSourceConceptIds")
    combined: list[str] = []
    if isinstance(prev_src, list):
        combined.extend(str(x) for x in prev_src if x)
    for x in ids:
        if x not in combined:
            combined.append(x)
    merged_data["kitsuSourceConceptIds"] = combined[:50]
    folder.data = merged_data
    await folder.save()
    existing_folders[str(new_kitsu_id)] = folder.id
    logging.info(
        "[concept_migrate] retargeted folder %s -> kitsuId prefix %s (n_sources=%s)",
        str(folder_id)[:8],
        str(new_kitsu_id)[:8],
        len(ids),
    )
    event = {
        "topic": "entity.folder.updated",
        "description": f"Folder {folder.name} updated",
        "summary": {"entityId": folder.id, "parentId": folder.parent_id},
        "project": project.name,
    }
    await dispatch_event(**event)
    return True


def _concept_folder_push_data(entity_dict: EntityDict) -> dict[str, Any]:
    data: dict[str, Any] = {"kitsuId": entity_dict["id"]}
    extra = entity_dict.get("kitsuSourceConceptIds")
    if isinstance(extra, list) and extra:
        data["kitsuSourceConceptIds"] = [str(x) for x in extra if x][:50]
    return data


async def merge_concept_folder_data_kitsu_fields(
    project_name: str,
    folder_id: str,
    entity_dict: EntityDict,
) -> None:
    """Merge ``kitsuSourceConceptIds`` / ``kitsuId`` on folder ``data`` (``update_folder`` skips ``data``)."""
    if entity_dict.get("type") != "Concept":
        return
    extras = entity_dict.get("kitsuSourceConceptIds")
    if not isinstance(extras, list) or not extras:
        return
    folder = await FolderEntity.load(project_name, folder_id)
    prev = dict(folder.data or {})
    data = dict(prev)
    data["kitsuId"] = str(entity_dict["id"])
    merged: list[str] = []
    ps = data.get("kitsuSourceConceptIds")
    if isinstance(ps, list):
        merged.extend(str(x) for x in ps if x)
    for x in extras:
        sx = str(x)
        if sx and sx not in merged:
            merged.append(sx)
    data["kitsuSourceConceptIds"] = merged[:50]
    if data == prev:
        return
    folder.data = data
    await folder.save()
    event = {
        "topic": "entity.folder.updated",
        "description": f"Folder {folder.name} updated",
        "summary": {"entityId": folder.id, "parentId": folder.parent_id},
        "project": project_name,
    }
    await dispatch_event(**event)


async def try_adopt_per_linked_concept_folder_on_duplicate_name(
    project: "ProjectEntity",
    parent_ayon_folder_id: str,
    entity_dict: "EntityDict",
    collision_folder_name: str,
    existing_folders: dict[str, Any],
    studio_settings: Any,
) -> FolderEntity | None:
    """When ``create_folder`` hits a name collision, merge into the existing Concept row.

    Covers races (two allocates saw an empty slot), legacy ``data.kitsuId`` on a
    sibling slug folder, and processors that omit ``__conceptSyncModel`` but studio
    settings use ``per_linked_entity``.
    """
    cs = getattr(studio_settings.sync_settings, "concept_sync", None)
    per_linked_meta = entity_dict.get("__conceptSyncModel") == "per_linked_entity"
    per_linked_setting = concept_entity_model_is_per_linked_entity(cs)
    if not per_linked_meta and not per_linked_setting:
        return None
    linked_id = str(entity_dict.get("id") or "").strip()
    if not linked_id:
        return None
    links = normalize_entity_concept_links(entity_dict.get("entity_concept_links"))
    effective_link = linked_id
    if concept_entity_model_is_per_linked_entity(cs) and len(links) == 1:
        only_l = str(links[0]).strip()
        if only_l:
            effective_link = only_l

    sources = [str(x) for x in (entity_dict.get("kitsuSourceConceptIds") or []) if x]
    if linked_id and linked_id != effective_link and linked_id not in sources:
        sources.append(linked_id)

    res = await Postgres.fetch(
        f"""
        SELECT id, COALESCE(data->>'kitsuId', '') AS kid
        FROM project_{project.name}.folders
        WHERE parent_id = $1 AND name = $2 AND folder_type = 'Concept'
        LIMIT 2
        """,
        parent_ayon_folder_id,
        collision_folder_name,
    )
    if len(res) != 1:
        return None
    folder_id = str(res[0]["id"])
    row_kid = str(res[0].get("kid") or "").strip()
    if row_kid and row_kid != effective_link and row_kid not in sources:
        if not (
            concept_entity_model_is_per_linked_entity(cs)
            and len(links) == 1
        ):
            logging.debug(
                f"[concept_adopt] name collision {collision_folder_name!r} "
                f"under parent={str(parent_ayon_folder_id)[:8]}: existing "
                f"kitsuId={(row_kid or '')[:12]!r} not mergeable into "
                f"effective_link={effective_link[:12]!r} (n_sources={len(sources)})"
            )
            return None

    folder = await FolderEntity.load(project.name, folder_id)
    new_data = dict(folder.data or {})
    new_data["kitsuId"] = effective_link
    merged: list[str] = []
    prev_src = new_data.get("kitsuSourceConceptIds")
    if isinstance(prev_src, list):
        merged.extend(str(x) for x in prev_src if x)
    for s in sources:
        if s not in merged:
            merged.append(s)
    if row_kid and row_kid not in merged:
        merged.append(row_kid)
    new_data["kitsuSourceConceptIds"] = merged[:50]
    folder.data = new_data
    await folder.save()
    existing_folders[effective_link] = folder_id
    logging.info(
        f"[concept_adopt] merged Concept folder {str(folder_id)[:8]} "
        f"name={collision_folder_name!r} -> kitsuId={str(effective_link)[:12]!r} "
        f"(prev_kitsuId={(row_kid or '')[:12]!r}, n_sources={len(merged)})"
    )
    event = {
        "topic": "entity.folder.updated",
        "description": f"Folder {folder.name} updated",
        "summary": {"entityId": folder.id, "parentId": folder.parent_id},
        "project": project.name,
    }
    await dispatch_event(**event)
    return folder


async def try_merge_per_kitsu_concept_folder_on_duplicate_slug(
    project: "ProjectEntity",
    parent_ayon_folder_id: str,
    entity_dict: "EntityDict",
    collision_folder_name: str,
    existing_folders: dict[str, Any],
    studio_settings: Any,
) -> FolderEntity | None:
    """When ``per_kitsu_concept`` is active, several concept rows can share one slug.

    The first ``create_folder`` wins; later rows collide on ``(parent_id, name)``.
    Merge the incoming Kitsu concept id into ``data.kitsuSourceConceptIds`` and
    register ``existing_folders`` so the push batch stays consistent.
    """
    cs = getattr(studio_settings.sync_settings, "concept_sync", None)
    if concept_entity_model_is_per_linked_entity(cs):
        return None
    if entity_dict.get("__conceptSyncModel") == "per_linked_entity":
        return None
    new_id = str(entity_dict.get("id") or "").strip()
    if not new_id:
        return None

    res = await Postgres.fetch(
        f"""
        SELECT id, COALESCE(data->>'kitsuId', '') AS kid
        FROM project_{project.name}.folders
        WHERE parent_id = $1 AND name = $2 AND folder_type = 'Concept'
        LIMIT 2
        """,
        parent_ayon_folder_id,
        collision_folder_name,
    )
    if len(res) != 1:
        return None
    folder_id = str(res[0]["id"])
    row_kid = str(res[0].get("kid") or "").strip()

    folder = await FolderEntity.load(project.name, folder_id)
    new_data = dict(folder.data or {})
    merged: list[str] = []
    prev_src = new_data.get("kitsuSourceConceptIds")
    if isinstance(prev_src, list):
        merged.extend(str(x) for x in prev_src if x)
    for x in entity_dict.get("kitsuSourceConceptIds") or []:
        sx = str(x).strip()
        if sx and sx not in merged:
            merged.append(sx)
    if new_id not in merged:
        merged.append(new_id)
    if row_kid and row_kid not in merged:
        merged.append(row_kid)
    new_data["kitsuSourceConceptIds"] = merged[:50]
    if not new_data.get("kitsuId") and row_kid:
        new_data["kitsuId"] = row_kid
    folder.data = new_data
    await folder.save()
    existing_folders[new_id] = folder_id
    if row_kid:
        existing_folders[row_kid] = folder_id
    logging.info(
        "[concept_merge_per_kitsu] merged duplicate slug %r folder=%s "
        "added_kitsu_id=%s (n_sources=%s)",
        collision_folder_name,
        str(folder_id)[:8],
        new_id[:12],
        len(merged),
    )
    event = {
        "topic": "entity.folder.updated",
        "description": f"Folder {folder.name} updated",
        "summary": {"entityId": folder.id, "parentId": folder.parent_id},
        "project": project.name,
    }
    await dispatch_event(**event)
    return folder


async def create_access_group(
    addon: "KitsuAddon",
    user: "UserEntity",
    entity_dict: "EntityDict",
    name: str | None = None,
):
    try:
        if not name:
            settings = await addon.get_studio_settings()
            name = settings.sync_settings.sync_users.access_group
        session = await Session.create(user)
        headers = {"Authorization": f"Bearer {session.token}"}
        # Check if group already exists
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{entity_dict['ayon_server_url']}/api/accessGroups/_",
                headers=headers,
            )

        for group in response.json():
            if group["name"] == name:
                # access group already exists
                return

        # Create a new access group
        payload = json.dumps(
            {
                "create": {"enabled": False, "access_list": []},
                "read": {"enabled": False, "access_list": []},
                "update": {"enabled": False, "access_list": []},
                "publish": {"enabled": False, "access_list": []},
                "delete": {"enabled": False, "access_list": []},
                "attrib_read": {"enabled": False, "attributes": []},
                "attrib_write": {"enabled": False, "attributes": []},
                "endpoints": {"enabled": False, "endpoints": []},
            }
        )

        async with httpx.AsyncClient() as client:
            return await client.put(
                f"{entity_dict['ayon_server_url']}/api/accessGroups/{name}/_",
                content=payload,
                headers=headers,
            )
    except Exception as e:
        print(e)


def match_ayon_roles_with_kitsu_role(role: str) -> dict[str, bool]:
    match role:
        case "admin":
            return {
                "isAdmin": True,
                "isManager": False,
            }
        case "manager":
            return {
                "isAdmin": False,
                "isManager": True,
            }
        case "user":
            return {
                "isAdmin": False,
                "isManager": False,
            }
        case _:
            return {}


async def generate_user_settings(
    addon: "KitsuAddon",
    entity_dict: "EntityDict",
):
    settings = await addon.get_studio_settings()
    data: dict[str, Any] = {}
    match entity_dict["role"]:
        case "admin":  # Studio manager
            data = match_ayon_roles_with_kitsu_role(
                settings.sync_settings.sync_users.roles.admin
            )
        case "vendor":  # Vendor
            data = match_ayon_roles_with_kitsu_role(
                settings.sync_settings.sync_users.roles.vendor
            )
        case "client":  # Client
            data = match_ayon_roles_with_kitsu_role(
                settings.sync_settings.sync_users.roles.client
            )
        case "manager":  # Manager
            data = match_ayon_roles_with_kitsu_role(
                settings.sync_settings.sync_users.roles.manager
            )
        case "supervisor":  # Supervisor
            data = match_ayon_roles_with_kitsu_role(
                settings.sync_settings.sync_users.roles.supervisor
            )
        case "user":  # Artist
            data = match_ayon_roles_with_kitsu_role(
                settings.sync_settings.sync_users.roles.user
            )
    return data | {
        "data": {
            "defaultAccessGroups": [
                settings.sync_settings.sync_users.access_group
            ],
        },
    }


async def sync_person(
    addon: "KitsuAddon",
    user: "UserEntity",
    existing_users: dict[str, Any],
    entity_dict: "EntityDict",
):

    first_name, entity_id= required_values(
        entity_dict, ["first_name", "id"]
    )
    last_name = entity_dict.get("last_name", '')

    # Skip if this person has already been processed in this batch
    # This prevents infinite loops if sync_person is called multiple times
    if entity_id in existing_users:
        logging.debug(
            f"sync_person: Skipping {first_name} {last_name} (id={entity_id}) - already processed"
        )
        return

    # Mark as processing immediately to prevent concurrent processing
    # We'll set the actual username value at the end
    existing_users[entity_id] = None

    # == check should Person entity be synced ==
    # do not sync Kitsu API bots
    if entity_dict.get("is_bot"):
        # Remove from processing map if we're skipping
        existing_users.pop(entity_id, None)
        return

    logging.debug(f"sync_person: {first_name} {last_name} (id={entity_id})")
    username = to_username(first_name, last_name)

    payload = {
        "name": username,
        "attrib": {
            "fullName": entity_dict.get("full_name", ""),
            "email": entity_dict.get("email", ""),
        },
    } | await generate_user_settings(
        addon,
        entity_dict,
    )
    payload["data"]["kitsuId"] = entity_id

    ayon_user = None
    try:
        ayon_user = await UserEntity.load(username)
    except Exception:
        pass
    target_user = await get_user_by_kitsu_id(entity_id)

    # User exists but doesn't have a kitsuId assigned it it
    if ayon_user and not target_user:
        target_user = ayon_user

    if target_user:  # Update user
        # Check if user data actually changed to avoid unnecessary updates
        user_changed = False

        # Check if name changed
        if target_user.name != username:
            user_changed = True

        # Check if email changed
        current_email = getattr(target_user.attrib, "email", "")
        new_email = payload.get("attrib", {}).get("email", "")
        if current_email != new_email:
            user_changed = True

        # Check if fullName changed
        current_full_name = getattr(target_user.attrib, "fullName", "")
        new_full_name = payload.get("attrib", {}).get("fullName", "")
        if current_full_name != new_full_name:
            user_changed = True

        # Check if kitsuId changed or is missing
        current_kitsu_id = target_user.data.get("kitsuId") if target_user.data else None
        if current_kitsu_id != entity_id:
            user_changed = True

        # Only update if something actually changed
        if not user_changed:
            logging.debug(
                f"sync_person: User {target_user.name} (id={entity_id}) unchanged, skipping update"
            )
            existing_users[entity_id] = target_user.name
            return

        try:
            session = await Session.create(user)
            headers = {"Authorization": f"Bearer {session.token}"}
            ayon_server_url = entity_dict["ayon_server_url"]
            async with httpx.AsyncClient() as client:
                await client.patch(
                    f"{ayon_server_url}/api/users/{target_user.name}",
                    json=payload,
                    headers=headers,
                )
            # Rename the user only if the username has changed
            # TODO: We should discourage renaming users.
            # Maybe just change the fullName in the case there's a typo,
            # but changing username may have weird side effects.
            if target_user.name != username:
                payload = {"newName": username}
                async with httpx.AsyncClient() as client:
                    await client.patch(
                        f"{ayon_server_url}/api/users/{target_user.name}/rename",
                        json=payload,
                        headers=headers,
                    )
        except Exception as e:
            logging.error(f"Error updating user {target_user.name}: {e}")
            # Remove from processing map to allow retry on next batch
            existing_users.pop(entity_id, None)
            return
    else:  # Create user
        # Double-check that user doesn't already exist before creating
        if ayon_user is None:
            try:
                ayon_user = await UserEntity.load(username)
            except Exception:
                pass

        if ayon_user:
            # User exists but wasn't found by kitsuId - use existing user
            target_user = ayon_user

            # Check if user data actually changed to avoid unnecessary updates
            user_changed = False

            # Check if email changed
            current_email = getattr(target_user.attrib, "email", "")
            new_email = payload.get("attrib", {}).get("email", "")
            if current_email != new_email:
                user_changed = True

            # Check if fullName changed
            current_full_name = getattr(target_user.attrib, "fullName", "")
            new_full_name = payload.get("attrib", {}).get("fullName", "")
            if current_full_name != new_full_name:
                user_changed = True

            # Check if kitsuId changed or is missing
            current_kitsu_id = target_user.data.get("kitsuId") if target_user.data else None
            if current_kitsu_id != entity_id:
                user_changed = True

            # Only update if something actually changed
            if not user_changed:
                logging.debug(
                    f"sync_person: User {target_user.name} (id={entity_id}) unchanged, skipping update"
                )
                existing_users[entity_id] = target_user.name
                return

            try:
                session = await Session.create(user)
                headers = {"Authorization": f"Bearer {session.token}"}
                ayon_server_url = entity_dict["ayon_server_url"]
                async with httpx.AsyncClient() as client:
                    await client.patch(
                        f"{ayon_server_url}/api/users/{target_user.name}",
                        json=payload,
                        headers=headers,
                    )
            except Exception as e:
                logging.error(f"Error updating existing user {target_user.name}: {e}")
                # Remove from processing map to allow retry on next batch
                existing_users.pop(entity_id, None)
                return
        else:
            # User doesn't exist - create new user
            try:
                new_user = UserEntity(payload)
                settings = await addon.get_studio_settings()
                new_user.set_password(settings.sync_settings.sync_users.default_password)
                await new_user.save()
            except Exception as e:
                logging.error(f"Error creating new user {username}: {e}")
                # Remove from processing map to allow retry on next batch
                existing_users.pop(entity_id, None)
                return

    # Update the id map with the actual username
    # (we set it to None earlier to mark as processing)
    existing_users[entity_id] = username


async def sync_project(
    addon: "KitsuAddon",
    user: "UserEntity",
    project: "ProjectEntity",
    entity_dict: "EntityDict",
    mock: bool = False,
):
    logging.info("sync_project")
    (entity_id,) = required_values(entity_dict, ["id"])

    if not project:
        logging.info("sync project not found")
        return

    # only sync if the project has the correct kitsu id stored on it.
    #   will succeed when paired correctly
    if project.data.get("kitsuProjectId") != entity_id:
        logging.info(
            f"project.data.kitsuProjectId {project.data.get('kitsuProjectId')}"
            f" not matching entity {entity_id}"
        )
        return

    await addon.ensure_kitsu(mock)
    anatomy = await get_kitsu_project_anatomy(addon, entity_id, project)
    anatomy_data = anatomy_to_project_data(anatomy)

    await update_project(project.name, **anatomy_data)


def _adopt_vizdev_task_data(
    task: TaskEntity,
    *,
    surrogate: str,
    concept_id: str,
    linked_entity_id: str | None = None,
) -> None:
    merged = dict(task.data or {})
    merged["kitsuId"] = surrogate
    merged["kitsuConceptId"] = concept_id
    merged["kitsuMirrorSlot"] = "VizDev"
    if linked_entity_id:
        merged["kitsuLinkedEntityId"] = linked_entity_id
    else:
        merged.pop("kitsuLinkedEntityId", None)
    task.data = merged


async def ensure_concept_vizdev_task(
    addon: "KitsuAddon",
    project: "ProjectEntity",
    entity_dict: "EntityDict",
    folder_id: str,
    existing_tasks: dict[str, Any],
) -> None:
    """Upsert one default task on a Concept folder for reviews / pipeline push alignment."""
    settings = await addon.get_studio_settings()
    cs = getattr(settings.sync_settings, "concept_sync", None)
    if cs is None or not getattr(cs, "enabled", True):
        return
    sync_meta = entity_dict.get("__conceptSyncModel")
    if sync_meta == "unlinked_hub":
        return

    linked_entity_id: str | None = None
    if sync_meta == "per_linked_entity":
        lid = str(entity_dict.get("id") or "")
        surrogate = concept_vizdev_surrogate_for_linked_entity(lid)
        linked_entity_id = lid
        contributors = [
            str(x) for x in (entity_dict.get("kitsuSourceConceptIds") or []) if x
        ]
        concept_id = contributors[0] if contributors else lid
    else:
        concept_id = str(entity_dict.get("id") or "")
        surrogate = concept_vizdev_surrogate_kitsu_id(concept_id)

    task_type_name = getattr(cs, "vizdev_task_type_name", None) or "VizDev"
    status_name = getattr(cs, "vizdev_task_status_name", None) or "todo"

    await ensure_task_status(project, status_name)
    await ensure_task_type(project, task_type_name)

    target_task = await get_task_by_kitsu_id(
        project.name,
        surrogate,
        existing_tasks,
    )
    if target_task is not None:
        existing_tasks[surrogate] = target_task.id
        t_existing = await TaskEntity.load(project.name, target_task.id)
        _adopt_vizdev_task_data(
            t_existing,
            surrogate=surrogate,
            concept_id=concept_id,
            linked_entity_id=linked_entity_id,
        )
        await t_existing.save()
        await update_task(
            project_name=project.name,
            task_id=target_task.id,
            name=task_type_name,
            status=status_name,
            task_type=task_type_name,
        )
        return

    adopt_id = await find_task_id_by_folder_name_type(
        project.name,
        folder_id,
        task_type_name,
        task_type_name,
    )
    if adopt_id is not None:
        logging.info(
            "Adopting existing folder task as VizDev surrogate for concept %s (task %s)",
            concept_id,
            adopt_id,
        )
        t = await TaskEntity.load(project.name, adopt_id)
        _adopt_vizdev_task_data(
            t,
            surrogate=surrogate,
            concept_id=concept_id,
            linked_entity_id=linked_entity_id,
        )
        await t.save()
        existing_tasks[surrogate] = adopt_id
        await update_task(
            project_name=project.name,
            task_id=adopt_id,
            name=task_type_name,
            status=status_name,
            task_type=task_type_name,
        )
        return

    logging.info(
        "Creating VizDev surrogate task for Concept %s (kitsuId=%r)",
        concept_id,
        surrogate,
    )
    task_data: dict[str, Any] = {
        "kitsuId": surrogate,
        "kitsuConceptId": concept_id,
        "kitsuMirrorSlot": "VizDev",
    }
    if linked_entity_id:
        task_data["kitsuLinkedEntityId"] = linked_entity_id
    try:
        new_task = await create_task(
            project_name=project.name,
            folder_id=folder_id,
            status=status_name,
            task_type=task_type_name,
            name=task_type_name,
            data=task_data,
            assignees=[],
        )
        existing_tasks[surrogate] = new_task.id
    except Exception as e:
        if not is_task_folder_name_unique_violation(e):
            raise
        retry_id = await find_task_id_by_folder_name_type(
            project.name,
            folder_id,
            task_type_name,
            task_type_name,
        )
        if retry_id is None:
            raise
        logging.warning(
            "VizDev create_task 409, adopting task %s for concept %s",
            retry_id,
            concept_id,
        )
        t = await TaskEntity.load(project.name, retry_id)
        _adopt_vizdev_task_data(
            t,
            surrogate=surrogate,
            concept_id=concept_id,
            linked_entity_id=linked_entity_id,
        )
        await t.save()
        existing_tasks[surrogate] = retry_id
        await update_task(
            project_name=project.name,
            task_id=retry_id,
            name=task_type_name,
            status=status_name,
            task_type=task_type_name,
        )


async def ensure_unlinked_concepts_pool_vizdev_task(
    addon: "KitsuAddon",
    project: "ProjectEntity",
    folder_id: str,
    existing_tasks: dict[str, Any],
) -> None:
    """Single VizDev surrogate for all unlinked Kitsu concepts under the Project anchor."""
    settings = await addon.get_studio_settings()
    cs = getattr(settings.sync_settings, "concept_sync", None)
    if cs is None or not getattr(cs, "enabled", True):
        return
    surrogate = concept_vizdev_surrogate_unlinked_pool()
    concept_id = "kitsu:concepts:unlinked_pool"
    task_type_name = getattr(cs, "vizdev_task_type_name", None) or "VizDev"
    status_name = getattr(cs, "vizdev_task_status_name", None) or "todo"

    await ensure_task_status(project, status_name)
    await ensure_task_type(project, task_type_name)

    target_task = await get_task_by_kitsu_id(
        project.name,
        surrogate,
        existing_tasks,
    )
    if target_task is not None:
        existing_tasks[surrogate] = target_task.id
        t_existing = await TaskEntity.load(project.name, target_task.id)
        _adopt_vizdev_task_data(
            t_existing,
            surrogate=surrogate,
            concept_id=concept_id,
            linked_entity_id=None,
        )
        await t_existing.save()
        await update_task(
            project_name=project.name,
            task_id=target_task.id,
            name=task_type_name,
            status=status_name,
            task_type=task_type_name,
        )
        return

    adopt_id = await find_task_id_by_folder_name_type(
        project.name,
        folder_id,
        task_type_name,
        task_type_name,
    )
    if adopt_id is not None:
        logging.info(
            "Adopting existing folder task as unlinked-pool VizDev surrogate (task %s)",
            adopt_id,
        )
        t = await TaskEntity.load(project.name, adopt_id)
        _adopt_vizdev_task_data(
            t,
            surrogate=surrogate,
            concept_id=concept_id,
            linked_entity_id=None,
        )
        await t.save()
        existing_tasks[surrogate] = adopt_id
        await update_task(
            project_name=project.name,
            task_id=adopt_id,
            name=task_type_name,
            status=status_name,
            task_type=task_type_name,
        )
        return

    logging.info(
        "Creating VizDev surrogate task for unlinked concept pool (kitsuId=%r)",
        surrogate,
    )
    task_data: dict[str, Any] = {
        "kitsuId": surrogate,
        "kitsuConceptId": concept_id,
        "kitsuMirrorSlot": "VizDev",
    }
    try:
        new_task = await create_task(
            project_name=project.name,
            folder_id=folder_id,
            status=status_name,
            task_type=task_type_name,
            name=task_type_name,
            data=task_data,
            assignees=[],
        )
        existing_tasks[surrogate] = new_task.id
    except Exception as e:
        if not is_task_folder_name_unique_violation(e):
            raise
        retry_id = await find_task_id_by_folder_name_type(
            project.name,
            folder_id,
            task_type_name,
            task_type_name,
        )
        if retry_id is None:
            raise
        logging.warning(
            "VizDev create_task 409 for unlinked pool, adopting task %s",
            retry_id,
        )
        t = await TaskEntity.load(project.name, retry_id)
        _adopt_vizdev_task_data(
            t,
            surrogate=surrogate,
            concept_id=concept_id,
            linked_entity_id=None,
        )
        await t.save()
        existing_tasks[surrogate] = retry_id
        await update_task(
            project_name=project.name,
            task_id=retry_id,
            name=task_type_name,
            status=status_name,
            task_type=task_type_name,
        )


async def delete_project(
    addon: "KitsuAddon",
    user: "UserEntity",
    project: "ProjectEntity",
    entity_dict: "EntityDict",
):
    logging.info("delete_project")
    session = await Session.create(user)
    headers = {"Authorization": f"Bearer {session.token}"}
    # Check if group already exists
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{entity_dict['ayon_server_url']}/api/projects/{project.name}",
            headers=headers,
        )


async def sync_folder(
    addon: "KitsuAddon",
    user: "UserEntity",
    project: "ProjectEntity",
    existing_folders: dict[str, Any],
    entity_dict: "EntityDict",
    existing_tasks: dict[str, Any] | None = None,
):
    target_folder = await get_folder_by_kitsu_id(
        project.name,
        entity_dict["id"],
        existing_folders,
    )

    studio_settings = await addon.get_studio_settings()
    concept_sanitize = True
    concept_title_src: EntityDict = entity_dict
    concept_ayon_ident: dict[str, str] | None = None

    # Concepts: title = linked entity names when enabled (Kitsu grid), else name/code.
    if entity_dict["type"] == "Concept":
        cs_name = getattr(studio_settings.sync_settings, "concept_sync", None)
        concept_sanitize = (
            cs_name is None
            or getattr(cs_name, "sanitize_kitsu_auto_naming", True)
        )
        prefer_linked = (
            cs_name is None
            or getattr(cs_name, "prefer_linked_asset_names", True)
        )
        concept_title_src = dict(entity_dict)
        raw_ln = concept_title_src.get("linked_entity_names")
        has_prefetched = isinstance(raw_ln, list) and any(
            str(x).strip() for x in raw_ln if x is not None
        )
        if (
            prefer_linked
            and concept_title_src.get("entity_concept_links")
            and not has_prefetched
        ):
            fetched = await _kitsu_fetch_linked_entity_names(
                addon, concept_title_src["entity_concept_links"]
            )
            if fetched:
                concept_title_src["linked_entity_names"] = fetched
        concept_kitsu_title = concept_primary_title_for_folder(concept_title_src)
        folder_label = concept_folder_display_name(
            concept_kitsu_title,
            sanitize=concept_sanitize,
        )
    else:
        raw_display = (entity_dict.get("name") or entity_dict.get("code") or "").strip()
        folder_label = raw_display or "folder"

    if (
        entity_dict["type"] == "Concept"
        and target_folder is None
        and entity_dict.get("parent_id") is None
    ):
        relinked = await try_relink_orphan_concept_folder(
            user=user,
            project=project,
            entity_dict=concept_title_src,
            existing_folders=existing_folders,
            sanitize_folder_display=concept_sanitize,
        )
        if relinked:
            target_folder = await get_folder_by_kitsu_id(
                project.name,
                entity_dict["id"],
                existing_folders,
            )

    if (
        entity_dict["type"] == "Concept"
        and target_folder is None
        and entity_dict.get("parent_id") is not None
    ):
        sub_relinked = await try_relink_orphan_concept_subfolder(
            user=user,
            project=project,
            entity_dict=concept_title_src,
            existing_folders=existing_folders,
            sanitize_folder_display=concept_sanitize,
        )
        if sub_relinked:
            target_folder = await get_folder_by_kitsu_id(
                project.name,
                entity_dict["id"],
                existing_folders,
            )

    if (
        entity_dict["type"] == "Concept"
        and target_folder is None
        and entity_dict.get("__conceptSyncModel") == "per_linked_entity"
    ):
        cs_migrate = getattr(studio_settings.sync_settings, "concept_sync", None)
        if concept_entity_model_is_per_linked_entity(cs_migrate):
            src_ids = list(entity_dict.get("kitsuSourceConceptIds") or [])
            if src_ids:
                parent_kitsu_m = entity_dict.get("parent_id")
                if parent_kitsu_m:
                    pfol_m = await get_folder_by_kitsu_id(
                        project.name,
                        str(parent_kitsu_m),
                        existing_folders,
                    )
                    parent_ayon_m = str(pfol_m.id) if pfol_m else ""
                else:
                    parent_ayon_m = await get_root_folder_id(
                        user=user,
                        project_name=project.name,
                        kitsu_type="Concepts",
                        kitsu_type_id="concept",
                    )
                if parent_ayon_m and await try_migrate_concept_folder_from_legacy_concept_ids(
                    project=project,
                    parent_ayon_folder_id=parent_ayon_m,
                    new_kitsu_id=str(entity_dict["id"]),
                    source_concept_ids=src_ids,
                    existing_folders=existing_folders,
                ):
                    target_folder = await get_folder_by_kitsu_id(
                        project.name,
                        entity_dict["id"],
                        existing_folders,
                    )

    # Add description to attrib data
    data: dict[str, str | int | None] | None = entity_dict.get("data", {})
    # The value of key data might be None, in that case, create a new dict
    if data is None:
        data = {}
    if entity_dict.get("description"):
        data["description"] = entity_dict["description"]
    if target_folder is None:
        parent_folder = None
        if entity_dict["type"] == "Asset":
            if entity_dict.get("entity_type_id") in existing_folders:
                parent_id = existing_folders[entity_dict["entity_type_id"]]
            else:
                parent_id = await get_root_folder_id(
                    user=user,
                    project_name=project.name,
                    kitsu_type="Assets",
                    kitsu_type_id="asset",
                    subfolder_id=entity_dict["entity_type_id"],
                    subfolder_name=entity_dict["asset_type_name"],
                )
                existing_folders[entity_dict["entity_type_id"]] = parent_id
        elif entity_dict["type"] in get_args(KitsuEntityType):
            if entity_dict.get("parent_id") is None:
                parent_id = await get_root_folder_id(
                    user=user,
                    project_name=project.name,
                    kitsu_type=f"{entity_dict['type']}s",
                    kitsu_type_id=entity_dict["type"].lower(),
                )
            else:
                if entity_dict.get("parent_id") in existing_folders:
                    parent_id = existing_folders[entity_dict["parent_id"]]
                else:
                    parent_folder = await get_folder_by_kitsu_id(
                        project.name,
                        entity_dict["parent_id"],
                        existing_folders,
                    )
                    if parent_folder is None:
                        logging.warning(
                            f"Parent folder for {entity_dict['type']}"
                            f" {entity_dict['name']} not found. Skipping."  # noqa
                        )
                        return
                    parent_id = parent_folder.id
        else:
            logging.warning("Unsupported entity type: ", entity_dict["type"])
            return
        # ensure folder type exists
        if entity_dict["type"] not in [
            f["name"]
            for f in project.folder_types
        ]:
            logging.warning(
                f"Folder type {entity_dict['type']} does not exist. Creating."
            )
            project.folder_types.append(
                {"name": entity_dict["type"]}
                | CONSTANT_KITSU_MODELS.get(entity_dict["type"], {})
            )
            await project.save()

        if entity_dict["type"] == "Concept":
            _cem = getattr(
                getattr(studio_settings.sync_settings, "concept_sync", None),
                "concept_entity_model",
                None,
            )
            logging.info(
                "Concept sync create project=%s kitsu_id=%s label=%r parent_id=%s "
                "__conceptSyncModel=%r studio.concept_entity_model=%r",
                project.name,
                str(entity_dict["id"]),
                folder_label,
                str(parent_id),
                entity_dict.get("__conceptSyncModel"),
                str(_cem) if _cem is not None else None,
            )
        else:
            logging.info(f"Creating {entity_dict['type']} {folder_label}")
        if not parent_folder:
            parent_folder = await FolderEntity.load(project.name, parent_id)
        # Calculate the end-frame
        data["frame_out"] = calculate_end_frame(entity_dict, parent_folder)

        if entity_dict["type"] == "Concept":
            concept_ayon_ident = await allocate_unique_concept_folder_name_label(
                project.name,
                str(parent_id),
                folder_label,
                str(entity_dict["id"]),
            )

        try:
            if entity_dict["type"] == "Concept" and concept_ayon_ident is not None:
                target_folder = await create_folder(
                    project_name=project.name,
                    attrib=parse_attrib(data),
                    name_and_label=concept_ayon_ident,
                    folder_type=entity_dict["type"],
                    parent_id=parent_id,
                    data=_concept_folder_push_data(entity_dict),
                )
            else:
                target_folder = await create_folder(
                    project_name=project.name,
                    attrib=parse_attrib(data),
                    name=folder_label,
                    folder_type=entity_dict["type"],
                    parent_id=parent_id,
                    data=(
                        _concept_folder_push_data(entity_dict)
                        if entity_dict["type"] == "Concept"
                        else {"kitsuId": entity_dict["id"]}
                    ),
                )
        except Exception as exc:
            if (
                entity_dict["type"] != "Concept"
                or not is_folder_parent_name_unique_violation(exc)
            ):
                raise
            target_folder = None
            rel2 = False
            if entity_dict.get("parent_id") is None:
                rel2 = await try_relink_orphan_concept_folder(
                    user=user,
                    project=project,
                    entity_dict=concept_title_src,
                    existing_folders=existing_folders,
                    sanitize_folder_display=concept_sanitize,
                )
            else:
                rel2 = await try_relink_orphan_concept_subfolder(
                    user=user,
                    project=project,
                    entity_dict=concept_title_src,
                    existing_folders=existing_folders,
                    sanitize_folder_display=concept_sanitize,
                )
            if rel2:
                target_folder = await get_folder_by_kitsu_id(
                    project.name,
                    entity_dict["id"],
                    existing_folders,
                )
            if (
                target_folder is None
                and concept_ayon_ident is not None
                and entity_dict["type"] == "Concept"
            ):
                target_folder = await try_adopt_per_linked_concept_folder_on_duplicate_name(
                    project,
                    str(parent_id),
                    entity_dict,
                    str(concept_ayon_ident["name"]),
                    existing_folders,
                    studio_settings,
                )
            if target_folder is None and concept_ayon_ident is not None:
                target_folder = await try_merge_per_kitsu_concept_folder_on_duplicate_slug(
                    project,
                    str(parent_id),
                    entity_dict,
                    str(concept_ayon_ident["name"]),
                    existing_folders,
                    studio_settings,
                )
            if target_folder is None and concept_ayon_ident is not None:
                concept_ayon_ident = await allocate_unique_concept_folder_name_label(
                    project.name,
                    str(parent_id),
                    folder_label,
                    str(entity_dict["id"]),
                )
                target_folder = await create_folder(
                    project_name=project.name,
                    attrib=parse_attrib(data),
                    name_and_label=concept_ayon_ident,
                    folder_type=entity_dict["type"],
                    parent_id=parent_id,
                    data=_concept_folder_push_data(entity_dict),
                )
            if target_folder is None:
                raise
        existing_folders[entity_dict["id"]] = target_folder.id

    else:
        # Calculate the end-frame
        data["frame_out"] = calculate_end_frame(entity_dict, target_folder)

        if entity_dict["type"] == "Concept":
            changed = await update_folder(
                project_name=project.name,
                folder_id=target_folder.id,
                attrib=parse_attrib(data),
                update_identifiers=False,
                folder_type=entity_dict["type"],
            )
            if changed:
                logging.info(
                    "Concept sync update attrib project=%s kitsu_id=%s "
                    "ayon_folder_id=%s",
                    project.name,
                    str(entity_dict["id"]),
                    str(target_folder.id),
                )
        else:
            changed = await update_folder(
                project_name=project.name,
                folder_id=target_folder.id,
                attrib=parse_attrib(data),
                name=folder_label,
                folder_type=entity_dict["type"],
            )
            if changed:
                logging.info(
                    f"Updating {entity_dict['type']} '{folder_label}'"
                )
        if changed:
            existing_folders[entity_dict["id"]] = target_folder.id

    if entity_dict["type"] == "Concept" and target_folder is not None:
        await merge_concept_folder_data_kitsu_fields(
            project.name,
            str(target_folder.id),
            entity_dict,
        )

    if (
        entity_dict["type"] == "Concept"
        and existing_tasks is not None
        and target_folder is not None
    ):
        await ensure_concept_vizdev_task(
            addon,
            project,
            entity_dict,
            target_folder.id,
            existing_tasks,
        )

    if (
        entity_dict.get("__conceptSyncModel") == "unlinked_project_anchor"
        and entity_dict["type"] == "Project"
        and existing_tasks is not None
        and target_folder is not None
    ):
        await ensure_unlinked_concepts_pool_vizdev_task(
            addon,
            project,
            str(target_folder.id),
            existing_tasks,
        )


async def ensure_task_type(
    project: "ProjectEntity",
    task_type_name: str,
) -> bool:
    """#TODO: kitsu listener for new task types would be preferable"""
    if task_type_name not in [
        task_type["name"]
        for task_type in project.task_types
    ]:
        logging.info(
            f"Creating task type {task_type_name} for '{project.name}'"
        )
        project.task_types.append(
            {
                "name": task_type_name,
                "shortName": task_type_name[:4],
                "icon": "task_alt",
            }
        )
        await project.save()
        return True
    return False


async def ensure_task_status(
    project: "ProjectEntity",
    task_status_name: str,
) -> bool:
    """#TODO: kitsu listener for new task statuses would be preferable"""

    if task_status_name not in [
        status["name"]
        for status in project.statuses
    ]:
        logging.info(
            f"Creating task status {task_status_name} for '{project.name}'"
        )
        project.statuses.append(
            {
                "name": task_status_name,
                "icon": "task_alt",
                "shortName": task_status_name[:4],
            }
        )
        await project.save()
        return True
    return False


async def sync_task(
    addon: "KitsuAddon",
    user: "UserEntity",
    project: "ProjectEntity",
    existing_tasks: dict[str, Any],
    existing_folders: dict[str, Any],
    entity_dict: "EntityDict",
):
    if "task_status_name" in entity_dict:
        await ensure_task_status(project, entity_dict["task_status_name"])

    if "task_type_name" in entity_dict:
        await ensure_task_type(project, entity_dict["task_type_name"])

    target_task = await get_task_by_kitsu_id(
        project.name,
        entity_dict["id"],
        existing_tasks,
    )

    if target_task is None:
        # Sync task
        if entity_dict.get("entity_id") in existing_folders:
            parent_id = existing_folders[entity_dict["entity_id"]]
        else:
            parent_folder = await get_folder_by_kitsu_id(
                project.name, entity_dict["entity_id"], existing_folders
            )

            if parent_folder:
                parent_id = parent_folder.id
            else:
                # The new task type haven't bin implemented in Ayon yet
                logging.warning(
                    f"The type '{entity_dict['name']}' isn't implemented yet."
                    f"Currently they aren't supported"
                )
                return

        logging.info(f"Creating {entity_dict['type']} '{entity_dict['name']}'")

        if "task_type_name" not in entity_dict:
            logging.warning(
                f"Task type not found for {entity_dict['name']}'"
            )
            return

        adopted_from_duplicate = False
        try:
            target_task = await create_task(
                project_name=project.name,
                folder_id=parent_id,
                status=entity_dict["task_status_name"],
                task_type=entity_dict["task_type_name"],
                name=entity_dict["name"],
                data={"kitsuId": entity_dict["id"]},
                assignees=entity_dict["assignees"],
            )
        except Exception as e:
            if not is_task_folder_name_unique_violation(e):
                raise
            stale_id = await find_task_id_by_folder_name_type(
                project.name,
                parent_id,
                entity_dict["name"],
                entity_dict["task_type_name"],
            )
            if stale_id is None:
                logging.warning(
                    "create_task hit unique-like error but could not resolve a single "
                    "task row (folder_id=%s name=%r type=%r): %s",
                    parent_id,
                    entity_dict["name"],
                    entity_dict["task_type_name"],
                    e,
                )
                raise
            target_task = await TaskEntity.load(project.name, stale_id)
            merged_data = dict(target_task.data or {})
            merged_data["kitsuId"] = entity_dict["id"]
            target_task.data = merged_data
            await target_task.save()
            adopted_from_duplicate = True
            logging.info(
                "Adopted existing AYON task %s for Kitsu id %s (folder_id=%s name=%r)",
                target_task.id,
                entity_dict["id"],
                parent_id,
                entity_dict["name"],
            )
        existing_tasks[entity_dict["id"]] = target_task.id

        if adopted_from_duplicate:
            changed = await update_task(
                project_name=project.name,
                task_id=target_task.id,
                name=entity_dict.get("name", target_task.name),
                assignees=entity_dict.get("assignees", target_task.assignees),
                status=entity_dict.get("task_status_name", target_task.status),
                task_type=entity_dict.get("task_type_name", target_task.task_type),
            )
            if changed:
                logging.info(
                    f"Updating {entity_dict['type']} '{entity_dict['name']}'"
                )

    else:
        changed = await update_task(
            project_name=project.name,
            task_id=target_task.id,
            name=entity_dict.get("name", target_task.name),
            assignees=entity_dict.get("assignees", target_task.assignees),
            status=entity_dict.get("task_status_name", target_task.status),
            task_type=entity_dict.get("task_type_name", target_task.task_type),
        )
        if changed:
            logging.info(
                f"Updating {entity_dict['type']} '{entity_dict['name']}'"
            )
            existing_tasks[entity_dict["id"]] = target_task.id


async def push_entities(
    addon: "KitsuAddon",
    user: "UserEntity",
    payload: PushEntitiesRequestModel,
) -> dict[str, dict[Any, Any]]:
    start_time = time.time()
    project = None
    if payload.project_name != "":
        project = await ProjectEntity.load(payload.project_name)

    # A mapping of kitsu entity ids to folder ids
    # they are added when a task or folder is created or updated and returned
    #   by the method - useful for testing

    # This object only exists during the request
    # and speeds up the process of finding folders
    # if multiple entities are requested to sync

    folders = {}
    tasks = {}
    users = {}

    settings = await addon.get_studio_settings()
    for entity_dict in payload.entities:
        # required fields
        assert "type" in entity_dict
        assert "id" in entity_dict

        if entity_dict["type"] not in get_args(KitsuEntityType):
            logging.warning(
                f"Unsupported kitsu entity type: {entity_dict['type']}"
            )
            continue

        if entity_dict["type"] == "Project":
            await sync_project(
                addon, user, project, entity_dict, payload.mock
            )
        elif entity_dict["type"] == "Person":
            if settings.sync_settings.sync_users.enabled:
                # Skip if this person has already been processed in this batch
                person_id = entity_dict.get("id")
                if person_id and person_id in users:
                    continue
                await create_access_group(
                    addon,
                    user,
                    entity_dict,
                )
                await sync_person(
                    addon,
                    user,
                    users,
                    entity_dict,
                )
        elif entity_dict["type"] == "Playlist":
            playlist_settings = getattr(
                settings.sync_settings, "playlist_sync", None
            )
            if playlist_settings is not None and getattr(
                playlist_settings, "enabled", False
            ):
                await sync_playlist_entity(addon, user, project, entity_dict)
            else:
                pl_id = entity_dict.get("id")
                pl_name = entity_dict.get("name")
                logging.warning(
                    f"push_entities: Playlist entity id={pl_id} name={pl_name!r} "
                    f"ignored — sync_settings.playlist_sync.enabled is false"
                )
        elif entity_dict["type"] != "Task":
            await sync_folder(
                addon,
                user,
                project,
                folders,
                entity_dict,
                existing_tasks=tasks,
            )
        else:
            logging.debug(
                f"push_entities: Syncing Task '{entity_dict.get('name')}' "
                f"with status '{entity_dict.get('task_status_name')}'"
            )
            await sync_task(
                addon,
                user,
                project,
                tasks,
                folders,
                entity_dict,
            )

    logging.info(
        f"Synced {len(payload.entities)}"
        f" entities in {time.time() - start_time}s"
    )

    # pass back the map of kitsu to ayon ids
    return {"folders": folders, "tasks": tasks, "users": users}


async def remove_entities(
    addon: "KitsuAddon",
    user: "UserEntity",
    payload: RemoveEntitiesRequestModel,
) -> dict[str, dict[Any, Any]]:
    start_time = time.time()
    project = await ProjectEntity.load(payload.project_name)

    # A mapping of kitsu entity ids to folder ids
    # they are added when a task or folder are deleted and returned
    #   by the method - useful for testing
    folders = {}
    tasks = {}

    settings = await addon.get_studio_settings()
    for entity_dict in payload.entities:
        if entity_dict["type"] not in get_args(KitsuEntityType):
            logging.warning(
                f"Unsupported kitsu entity type: {entity_dict['type']}"
            )
            continue

        if entity_dict["type"] == "Project":
            if settings.delete_ayon_projects.enabled:
                await update_project(
                    addon,
                    user,
                    project,
                    entity_dict,
                )
        elif entity_dict["type"] == "Person":
            target_user = await get_user_by_kitsu_id(entity_dict["id"])
            if not target_user:
                continue

            await target_user.delete()

        elif entity_dict["type"] == "Playlist":
            playlist_settings = getattr(
                settings.sync_settings, "playlist_sync", None
            )
            if playlist_settings is not None and getattr(
                playlist_settings, "enabled", False
            ):
                await delete_playlist_entity(addon, user, project, entity_dict)
            else:
                pl_id = entity_dict.get("id")
                pl_name = entity_dict.get("name")
                logging.warning(
                    f"remove_entities: Playlist entity id={pl_id} name={pl_name!r} "
                    f"ignored — sync_settings.playlist_sync.enabled is false"
                )

        elif entity_dict["type"] == "Task":
            task = await get_task_by_kitsu_id(
                project.name,
                entity_dict["id"],
                tasks,
            )
            if not task:
                continue

            await delete_task(
                project_name=project.name,
                task_id=task.id,
                user=user,
            )
            logging.info(f"Deleted {entity_dict['type']} '{task.name}'")
            tasks[entity_dict["id"]] = task.id

        else:
            if entity_dict["type"] == "Concept":
                surrogate = concept_vizdev_surrogate_kitsu_id(entity_dict["id"])
                viz_task = await get_task_by_kitsu_id(
                    project.name,
                    surrogate,
                    tasks,
                )
                if viz_task:
                    await delete_task(
                        project_name=project.name,
                        task_id=viz_task.id,
                        user=user,
                    )
                    tasks[surrogate] = viz_task.id
            folder = await get_folder_by_kitsu_id(
                project.name,
                entity_dict["id"],
                folders,
            )
            if not folder:
                continue

            await delete_folder(
                project_name=project.name,
                folder_id=folder.id,
                user=user,
            )
            logging.info(f"Deleted {entity_dict['type']} '{folder.name}'")
            folders[entity_dict["id"]] = folder.id

    logging.info(
        f"Deleted {len(payload.entities)} entities"
        f" in {time.time() - start_time}s"
    )

    # pass back the map of kitsu to ayon ids
    return {"folders": folders, "tasks": tasks}
