from typing import TYPE_CHECKING

import ayon_api
import gazu
from nxtools import log_traceback, logging

from . import content_sync, utils
from .playlist_push_entity import (
    build_playlist_push_entity,
    playlist_sync_enabled,
)
from .sync_error_format import format_entity_sync_headline
from .sync_events import emit_sync_entity_failed, parse_http_error_detail
from .task_relink import kitsu_folder_map_from_ayon_project, push_entities_with_relink

if TYPE_CHECKING:
    from .processor import KitsuProcessor


def update_project(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] update_project: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.project.get_project(data["project_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated project {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update project {project_name}: {e}")
        log_traceback(f"Error updating project {project_name}")


def delete_project(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_project: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping project deletion")
        return

    try:
        entity = {"ayon_server_url": ayon_api.get_base_url()}

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted project {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete project {project_name}: {e}")
        log_traceback(f"Error deleting project {project_name}")


def create_or_update_asset(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_asset: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping asset {data.get('asset_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.asset.get_asset(data["asset_id"])
        entity = utils.preprocess_asset(entity["project_id"], entity)
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated asset {data.get('asset_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update asset {data.get('asset_id')} in {project_name}: {e}")
        log_traceback(f"Error updating asset {data.get('asset_id')}")


def delete_asset(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_asset: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping asset deletion {data.get('asset_id')}")
        return

    try:
        entity = {
            "id": data["asset_id"],
            "type": "Asset",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted asset {data.get('asset_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete asset {data.get('asset_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting asset {data.get('asset_id')}")


def create_or_update_episode(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_episode: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping episode {data.get('episode_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.shot.get_episode(data["episode_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated episode {data.get('episode_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update episode {data.get('episode_id')} in {project_name}: {e}")
        log_traceback(f"Error updating episode {data.get('episode_id')}")


def delete_episode(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_episode: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping episode deletion {data.get('episode_id')}")
        return

    try:
        entity = {
            "id": data["episode_id"],
            "type": "Episode",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted episode {data.get('episode_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete episode {data.get('episode_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting episode {data.get('episode_id')}")


def create_or_update_sequence(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_sequence: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping sequence {data.get('sequence_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.shot.get_sequence(data["sequence_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated sequence {data.get('sequence_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update sequence {data.get('sequence_id')} in {project_name}: {e}")
        log_traceback(f"Error updating sequence {data.get('sequence_id')}")


def delete_sequence(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_sequence: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping sequence deletion {data.get('sequence_id')}")
        return

    try:
        entity = {
            "id": data["sequence_id"],
            "type": "Sequence",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted sequence {data.get('sequence_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete sequence {data.get('sequence_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting sequence {data.get('sequence_id')}")


def create_or_update_shot(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_shot: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping shot {data.get('shot_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.shot.get_shot(data["shot_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated shot {data.get('shot_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update shot {data.get('shot_id')} in {project_name}: {e}")
        log_traceback(f"Error updating shot {data.get('shot_id')}")


def delete_shot(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_shot: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping shot deletion {data.get('shot_id')}")
        return

    try:
        entity = {
            "id": data["shot_id"],
            "type": "Shot",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted shot {data.get('shot_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete shot {data.get('shot_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting shot {data.get('shot_id')}")


def create_or_update_task(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_task: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping task {data.get('task_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    entity: dict | None = None
    try:
        entity = gazu.task.get_task(data["task_id"])
        entity = utils.preprocess_task(entity["project_id"], entity)
        entity["ayon_server_url"] = ayon_api.get_base_url()

        folder_map: dict[str, str] = {}
        push_entities_with_relink(
            parent.entrypoint,
            project_name,
            [entity],
            folder_map,
        )
        logging.info(
            f"[update_from_kitsu] Successfully updated task {data.get('task_id')} in {project_name}"
        )
    except Exception as e:
        task_id = str(data.get("task_id", "unknown"))
        task_name = "unknown"
        if entity is not None:
            task_name = str(entity.get("name", "unknown"))
        parsed = parse_http_error_detail(e)
        headline = format_entity_sync_headline(
            project_name, "Task", task_name, task_id, parsed
        )
        logging.error(f"[update_from_kitsu] {headline}")
        log_traceback(f"Incremental task {task_id}")
        emit_sync_entity_failed(
            project_name,
            headline,
            {
                "phase": "incremental_task",
                "entityType": "Task",
                "entityName": task_name,
                "kitsuEntityId": task_id,
                "kitsuTaskId": task_id,
                "projectId": str(data.get("project_id", "")),
            },
            payload=parsed,
        )


def delete_task(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_task: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping task deletion {data.get('task_id')}")
        return

    try:
        entity = {
            "id": data["task_id"],
            "type": "Task",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted task {data.get('task_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete task {data.get('task_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting task {data.get('task_id')}")


def create_or_update_edit(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_edit: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping edit {data.get('edit_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.edit.get_edit(data["edit_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated edit {data.get('edit_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update edit {data.get('edit_id')} in {project_name}: {e}")
        log_traceback(f"Error updating edit {data.get('edit_id')}")


def delete_edit(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_edit: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping edit deletion {data.get('edit_id')}")
        return

    try:
        entity = {
            "id": data["edit_id"],
            "type": "Edit",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted edit {data.get('edit_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete edit {data.get('edit_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting edit {data.get('edit_id')}")


def create_or_update_concept(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_concept: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping concept {data.get('concept_id')}")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        _cs = (parent.settings.get("sync_settings") or {}).get("concept_sync")
        concept_sync = utils.normalize_concept_sync_dict(
            _cs if isinstance(_cs, dict) else None,
        )
        entity = utils.load_concept_entity_for_sync(
            str(data["project_id"]),
            str(data["concept_id"]),
            data.get("parent_id"),
            concept_sync=concept_sync,
        )
        entity["ayon_server_url"] = ayon_api.get_base_url()

        rows = utils.expand_single_concept_entity_for_push(
            entity,
            concept_sync=concept_sync,
        )
        folder_map: dict[str, str] = {}
        push_entities_with_relink(
            parent.entrypoint,
            project_name,
            rows,
            folder_map,
            concept_sync=concept_sync,
        )
        cid = str(data["concept_id"])
        pid = str(data["project_id"])
        source_concept_for_preview = cid
        links0 = entity.get("entity_concept_links") or []
        has_concept_links = isinstance(links0, (list, tuple)) and any(
            str(x).strip() for x in links0 if x is not None
        )
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("__conceptSyncModel") in (
                "unlinked_hub",
                "unlinked_project_anchor",
            ):
                continue
            pf = row.get("preview_file_id")
            if not pf:
                continue
            sync_m = row.get("__conceptSyncModel")
            if sync_m == "per_linked_entity":
                lid = str(row.get("id") or "")
                if lid:
                    surrogate = content_sync.concept_vizdev_surrogate_for_linked_entity(
                        lid,
                    )
                    try:
                        content_sync.sync_preview_to_ayon(
                            parent,
                            str(pf),
                            surrogate,
                            pid,
                            source_kitsu_concept_id=source_concept_for_preview,
                        )
                    except Exception as sync_exc:
                        logging.warning(
                            "[update_from_kitsu] concept preview sync failed "
                            "linked_entity=%s: %s",
                            lid[:8],
                            sync_exc,
                        )
            else:
                preview_cid = str(row.get("id") or cid)
                surrogate = content_sync.concept_vizdev_surrogate_kitsu_id(preview_cid)
                try:
                    content_sync.sync_preview_to_ayon(
                        parent, str(pf), surrogate, pid,
                    )
                except Exception as sync_exc:
                    logging.warning(
                        "[update_from_kitsu] concept preview sync failed concept=%s: %s",
                        preview_cid,
                        sync_exc,
                    )
        if (
            utils.unlinked_concepts_anchor_is_project(concept_sync)
            and utils.concept_entity_model_is_per_linked_dict(concept_sync)
            and not has_concept_links
            and entity.get("preview_file_id")
        ):
            pool_sur = content_sync.concept_vizdev_surrogate_unlinked_pool()
            try:
                content_sync.sync_preview_to_ayon(
                    parent,
                    str(entity["preview_file_id"]),
                    pool_sur,
                    pid,
                    source_kitsu_concept_id=cid,
                )
            except Exception as sync_exc:
                logging.warning(
                    "[update_from_kitsu] concept preview sync failed "
                    "unlinked_pool: %s",
                    sync_exc,
                )
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("__conceptSyncModel") in (
                "unlinked_hub",
                "unlinked_project_anchor",
            ):
                continue
            try:
                content_sync.sync_thumbnail_to_ayon(parent, row, project_name)
            except Exception as thumb_exc:
                logging.warning(
                    "[update_from_kitsu] concept thumbnail sync failed entity=%s: %s",
                    row.get("id"),
                    thumb_exc,
                )
        if (
            utils.unlinked_concepts_anchor_is_project(concept_sync)
            and utils.concept_entity_model_is_per_linked_dict(concept_sync)
            and not has_concept_links
            and entity.get("preview_file_id")
        ):
            anchor = (
                (concept_sync or {}).get("unlinked_concepts_project_kitsu_id")
                or utils.DEFAULT_UNLINKED_PROJECT_KITSU_ID
            )
            thumb_row = {
                "id": str(anchor),
                "preview_file_id": entity["preview_file_id"],
            }
            try:
                content_sync.sync_thumbnail_to_ayon(parent, thumb_row, project_name)
            except Exception as thumb_exc:
                logging.warning(
                    "[update_from_kitsu] concept thumbnail sync failed "
                    "unlinked_project anchor: %s",
                    thumb_exc,
                )
        logging.info(f"[update_from_kitsu] Successfully updated concept {data.get('concept_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update concept {data.get('concept_id')} in {project_name}: {e}")
        log_traceback(f"Error updating concept {data.get('concept_id')}")


def delete_concept(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_concept: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping concept deletion {data.get('concept_id')}")
        return

    try:
        entity = {
            "id": data["concept_id"],
            "type": "Concept",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted concept {data.get('concept_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete concept {data.get('concept_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting concept {data.get('concept_id')}")


def create_or_update_playlist(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_playlist: {data}")
    if not playlist_sync_enabled(parent):
        logging.debug("[update_from_kitsu] playlist sync disabled, skipping")
        return
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(
            f"[update_from_kitsu] Project {data.get('project_id')} not paired, "
            f"skipping playlist {data.get('playlist_id')}"
        )
        return
    playlist_id = data.get("playlist_id") or data.get("id")
    if not playlist_id:
        logging.warning("[update_from_kitsu] playlist event missing playlist_id")
        return
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        full = gazu.playlist.get_playlist(playlist_id)
        folder_map = kitsu_folder_map_from_ayon_project(project_name)
        entity = build_playlist_push_entity(
            project_name, full, folder_map, ayon_api.get_base_url()
        )
        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(
            f"[update_from_kitsu] Successfully synced playlist {playlist_id} "
            f"in {project_name}"
        )
    except Exception as e:
        logging.error(
            f"[update_from_kitsu] Failed to sync playlist {playlist_id} "
            f"in {project_name}: {e}"
        )
        log_traceback(f"Error syncing playlist {playlist_id}")


def delete_playlist(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_playlist: {data}")
    if not playlist_sync_enabled(parent):
        return
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(
            f"[update_from_kitsu] Project {data.get('project_id')} not paired, "
            f"skipping playlist delete {data.get('playlist_id')}"
        )
        return
    playlist_id = data.get("playlist_id") or data.get("id")
    if not playlist_id:
        return
    try:
        entity = {
            "id": playlist_id,
            "type": "Playlist",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(
            f"[update_from_kitsu] Successfully removed playlist {playlist_id} "
            f"from {project_name}"
        )
    except Exception as e:
        logging.error(
            f"[update_from_kitsu] Failed to delete playlist {playlist_id} "
            f"from {project_name}: {e}"
        )
        log_traceback(f"Error deleting playlist {playlist_id}")


def create_or_update_person(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] create_or_update_person: {data}")
    utils.set_kitsu_host(parent.kitsu_server_url)
    try:
        entity = gazu.person.get_person(data["person_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name="",
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated person {data.get('person_id')}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update person {data.get('person_id')}: {e}")
        log_traceback(f"Error updating person {data.get('person_id')}")


def delete_person(parent: "KitsuProcessor", data: dict[str, str]):
    logging.info(f"[update_from_kitsu] delete_person: {data}")
    project_name = parent.get_paired_ayon_project(data.get("project_id"))
    if not project_name:
        logging.debug(f"[update_from_kitsu] Project {data.get('project_id')} not paired, skipping person deletion {data.get('person_id')}")
        return

    try:
        entity = {
            "id": data["person_id"],
            "type": "person",
            "ayon_server_url": ayon_api.get_base_url(),
        }
        response = ayon_api.post(
            f"{parent.entrypoint}/remove",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully deleted person {data.get('person_id')} from {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to delete person {data.get('person_id')} from {project_name}: {e}")
        log_traceback(f"Error deleting person {data.get('person_id')}")
