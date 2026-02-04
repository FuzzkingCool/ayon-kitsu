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
    set_kitsu_host,
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

    logging.debug(f"[fullsync] parent.kitsu_server_url = {parent.kitsu_server_url!r}")
    
    if parent.kitsu_server_url is None:
        raise RuntimeError(
            "Cannot perform fullsync: Kitsu server URL is not initialized. "
            "This usually means Kitsu settings are not configured."
        )
    
    try:
        logging.debug(f"[fullsync] gazu.get_host() before set = {gazu.get_host()!r}")
        
        # Set thread-local so utils._ensure_gazu_host() uses correct URL before every gazu call
        set_kitsu_host(parent.kitsu_server_url)
        parent._ensure_gazu_host(parent.kitsu_server_url)
        
        logging.debug(f"[fullsync] gazu.get_host() after set = {gazu.get_host()!r}")
        logging.info(f"[fullsync] Using Kitsu server: {parent.kitsu_server_url!r}")
    except Exception as e:
        logging.error(f"[fullsync] Failed to set Kitsu server URL: {e}")
        raise

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

    # Add AYON server URL to each entity
    for entity in entities:
        entity["ayon_server_url"] = ayon_api.get_base_url()

    # Chunk the entities into smaller batches (e.g., 100 entities per batch)
    batch_size = 100
    total_batches = (len(entities) + batch_size - 1) // batch_size
    logging.info(f"[fullsync] Processing {total_batches} batches of {batch_size} entities each")

    # Track progress and failures
    processed_count = 0
    failed_batches = []

    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min((batch_num + 1) * batch_size, len(entities))
        batch = entities[start_idx:end_idx]
        
        logging.info(f"[fullsync] Sending batch {batch_num + 1}/{total_batches} ({len(batch)} entities)")

        try:
            response = ayon_api.post(
                f"{parent.entrypoint}/push",
                project_name=project_name,
                entities=batch,
            )
            response.raise_for_status()
            processed_count += len(batch)
            logging.info(f"[fullsync] Batch {batch_num + 1} processed successfully")
        except Exception as e:
            logging.error(
                f"[fullsync] Failed to push batch {batch_num + 1} for project {project_name}: {e}"
            )
            log_traceback(f"Error pushing batch {batch_num + 1} for {project_name}")
            failed_batches.append((batch, e))

    # Log summary
    logging.info(
        f"[fullsync] Processed {processed_count}/{len(entities)} entities successfully"
    )
    if failed_batches:
        logging.warning(f"[fullsync] {len(failed_batches)} batches failed. Retrying...")
        # Retry failed batches
        for batch, error in failed_batches:
            max_retries = 3
            retry_delay = 5  # seconds
            
            for retry in range(max_retries):
                try:
                    logging.info(f"[fullsync] Retrying batch (attempt {retry + 1}/{max_retries})")
                    response = ayon_api.post(
                        f"{parent.entrypoint}/push",
                        project_name=project_name,
                        entities=batch,
                    )
                    response.raise_for_status()
                    processed_count += len(batch)
                    logging.info(f"[fullsync] Batch retried successfully on attempt {retry + 1}")
                    break
                except Exception as e:
                    logging.error(
                        f"[fullsync] Retry {retry + 1} failed for batch: {e}"
                    )
                    time.sleep(retry_delay)
            else:
                logging.error(f"[fullsync] Batch failed after {max_retries} retries")

    # Final summary
    if processed_count == len(entities):
        logging.info(
            f"[fullsync] Full Sync for project {project_name} "
            f"completed successfully in {time.time() - start_time:.2f}s"
        )
    else:
        logging.warning(
            f"[fullsync] Sync for project {project_name} completed with {len(entities) - processed_count} entities failed"
        )
