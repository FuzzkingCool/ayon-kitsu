import time
from typing import TYPE_CHECKING

import ayon_api
import gazu
from nxtools import log_traceback, logging

if TYPE_CHECKING:
    from .processor import KitsuProcessor

from .utils import (
    get_asset_types,
    get_statuses,
    get_task_types,
    preprocess_asset,
    preprocess_task,
)


def get_assets(
    kitsu_project_id: str, asset_types: dict[str, str]
) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    try:
        records = gazu.asset.all_assets_for_project(kitsu_project_id)
        for record in records:
            try:
                assets.append(
                    preprocess_asset(kitsu_project_id, record, asset_types)
                )
            except Exception as e:
                logging.error(
                    f"[fullsync] Failed to preprocess asset {record.get('id', 'unknown')}: {e}"
                )
                log_traceback(
                    f"Asset preprocessing error for {record.get('id', 'unknown')}"
                )
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get assets for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting assets for project {kitsu_project_id}")
        raise
    return assets


def get_tasks(
    kitsu_project_id: str,
    task_types: dict[str, str],
    task_statuses: dict[str, str],
) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    try:
        records = gazu.task.all_tasks_for_project(kitsu_project_id)
        for record in records:
            try:
                record["persons"]: list[dict[str, str]] = []
                for person_id in record.get("assignees", []):
                    try:
                        person = gazu.person.get_person(person_id)
                        record["persons"].append(
                            {"email": person.get("email", "")}
                        )
                    except Exception as e:
                        logging.warning(
                            f"[fullsync] Failed to get person {person_id} for task {record.get('id', 'unknown')}: {e}"
                        )
                tasks.append(
                    preprocess_task(
                        kitsu_project_id, record, task_types, task_statuses
                    )
                )
            except Exception as e:
                logging.error(
                    f"[fullsync] Failed to preprocess task {record.get('id', 'unknown')}: {e}"
                )
                log_traceback(
                    f"Task preprocessing error for {record.get('id', 'unknown')}"
                )
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get tasks for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting tasks for project {kitsu_project_id}")
        raise
    return tasks


def project_full_sync(
    parent: "KitsuProcessor", kitsu_project_id: str, project_name: str
):
    """Sync all entities from a Kitsu project to an Ayon project.

    Args:
        parent (KitsuProcessor): The parent processor
        kitsu_project_id (str): The Kitsu project id
        project_name (str): The Ayon project name

    Raises:
        Exception: If any critical step fails during sync
    """
    start_time = time.time()
    logging.info(
        f"[fullsync] Starting sync: Kitsu project {kitsu_project_id} -> Ayon project {project_name}"
    )

    try:
        asset_types = get_asset_types(kitsu_project_id)
        logging.debug(f"[fullsync] Retrieved {len(asset_types)} asset types")
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get asset types for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting asset types for {kitsu_project_id}")
        raise

    try:
        task_statuses = get_statuses()
        logging.debug(
            f"[fullsync] Retrieved {len(task_statuses)} task statuses"
        )
    except Exception as e:
        logging.error(f"[fullsync] Failed to get task statuses: {e}")
        log_traceback("Error getting task statuses")
        raise

    try:
        task_types = get_task_types(kitsu_project_id)
        logging.debug(f"[fullsync] Retrieved {len(task_types)} task types")
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get task types for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting task types for {kitsu_project_id}")
        raise

    try:
        persons = gazu.person.all_persons()
        logging.debug(f"[fullsync] Retrieved {len(persons)} persons")
    except Exception as e:
        logging.error(f"[fullsync] Failed to get persons: {e}")
        log_traceback("Error getting persons")
        raise

    try:
        assets = get_assets(kitsu_project_id, asset_types)
        logging.info(f"[fullsync] Retrieved {len(assets)} assets")
    except Exception as e:
        logging.error(f"[fullsync] Failed to get assets: {e}")
        log_traceback("Error getting assets")
        raise

    try:
        tasks = get_tasks(kitsu_project_id, task_types, task_statuses)
        logging.info(f"[fullsync] Retrieved {len(tasks)} tasks")
    except Exception as e:
        logging.error(f"[fullsync] Failed to get tasks: {e}")
        log_traceback("Error getting tasks")
        raise

    try:
        episodes = gazu.shot.all_episodes_for_project(kitsu_project_id)
        logging.info(f"[fullsync] Retrieved {len(episodes)} episodes")
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get episodes for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting episodes for {kitsu_project_id}")
        raise

    try:
        seqs = gazu.shot.all_sequences_for_project(kitsu_project_id)
        logging.info(f"[fullsync] Retrieved {len(seqs)} sequences")
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get sequences for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting sequences for {kitsu_project_id}")
        raise

    try:
        shots = gazu.shot.all_shots_for_project(kitsu_project_id)
        logging.info(f"[fullsync] Retrieved {len(shots)} shots")
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get shots for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting shots for {kitsu_project_id}")
        raise

    try:
        edits = gazu.edit.all_edits_for_project(kitsu_project_id)
        logging.info(f"[fullsync] Retrieved {len(edits)} edits")
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to get edits for project {kitsu_project_id}: {e}"
        )
        log_traceback(f"Error getting edits for {kitsu_project_id}")
        raise

    # Concepts were introduced at Kitsu/Zou v0.18.0.
    # If the user runs an older version if Kitsu, gazu.concept will
    #    throw an error.
    concepts = []
    try:
        concepts = gazu.concept.all_concepts_for_project(kitsu_project_id)
        logging.info(f"[fullsync] Retrieved {len(concepts)} concepts")
    except Exception as e:
        logging.debug(
            f"[fullsync] Concepts not available (may be older Kitsu version): {e}"
        )

    entities = (
        persons + assets + episodes + seqs + shots + edits + concepts + tasks
    )
    logging.info(f"[fullsync] Total entities to sync: {len(entities)}")

    for entity in entities:
        entity["ayon_server_url"] = ayon_api.get_base_url()

    try:
        logging.info(
            f"[fullsync] Sending {len(entities)} entities to Ayon server..."
        )
        response = ayon_api.post(
            f"{parent.entrypoint}/push",
            project_name=project_name,
            entities=entities,
        )
        response.raise_for_status()
        logging.info(
            f"[fullsync] Full Sync for project {project_name} "
            f"completed successfully in {time.time() - start_time:.2f}s"
        )
    except Exception as e:
        logging.error(
            f"[fullsync] Failed to push entities to Ayon server for project {project_name}: {e}"
        )
        log_traceback(f"Error pushing entities to Ayon for {project_name}")
        raise
