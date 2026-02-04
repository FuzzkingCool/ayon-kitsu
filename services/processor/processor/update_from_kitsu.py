from typing import TYPE_CHECKING

import ayon_api
import gazu
from nxtools import log_traceback, logging

from . import utils

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
    try:
        entity = gazu.task.get_task(data["task_id"])
        entity = utils.preprocess_task(entity["project_id"], entity)
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
        logging.info(f"[update_from_kitsu] Successfully updated task {data.get('task_id')} in {project_name}")
    except Exception as e:
        logging.error(f"[update_from_kitsu] Failed to update task {data.get('task_id')} in {project_name}: {e}")
        log_traceback(f"Error updating task {data.get('task_id')}")


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
        entity = gazu.concept.get_concept(data["concept_id"])
        entity["ayon_server_url"] = ayon_api.get_base_url()

        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=[entity],
        )
        response.raise_for_status()
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
