import time
from typing import TYPE_CHECKING

import ayon_api
import gazu
from nxtools import log_traceback, logging

if TYPE_CHECKING:
    from .processor import KitsuProcessor

from .sync_events import emit_sync_entity_failed, emit_sync_summary, parse_http_error_detail
from .task_relink import merge_push_response_folder_map, push_entities_with_relink
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
    persons_by_id: dict[str, dict],
    ayon_users_by_email: dict[str, str],
) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    try:
        logging.debug(f"[fullsync] Calling gazu.task.all_tasks_for_project...")
        records = gazu.task.all_tasks_for_project(kitsu_project_id)
        logging.debug(
            f"[fullsync] Got {len(records)} task records, processing..."
        )
        for idx, record in enumerate(records):
            if idx % 10 == 0:
                logging.debug(
                    f"[fullsync] Processing task {idx}/{len(records)}"
                )
            try:
                record["persons"]: list[dict[str, str]] = []
                for person_id in record.get("assignees", []):
                    person = persons_by_id.get(person_id)
                    if person:
                        record["persons"].append(
                            {"email": person.get("email", "")}
                        )
                    else:
                        logging.warning(
                            f"[fullsync] Person {person_id} not found in persons list for task {record.get('id', 'unknown')}"
                        )
                tasks.append(
                    preprocess_task(
                        kitsu_project_id,
                        record,
                        task_types,
                        task_statuses,
                        ayon_users_by_email,
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

    logging.debug(
        f"[fullsync] parent.kitsu_server_url = {parent.kitsu_server_url!r}"
    )

    if parent.kitsu_server_url is None:
        raise RuntimeError(
            "Cannot perform fullsync: Kitsu server URL is not initialized. "
            "This usually means Kitsu settings are not configured."
        )

    try:
        logging.debug(
            f"[fullsync] gazu.get_host() before set = {gazu.get_host()!r}"
        )

        # Set thread-local so utils._ensure_gazu_host() uses correct URL before every gazu call
        set_kitsu_host(parent.kitsu_server_url)
        parent._ensure_gazu_host(parent.kitsu_server_url)

        logging.debug(
            f"[fullsync] gazu.get_host() after set = {gazu.get_host()!r}"
        )
        logging.info(
            f"[fullsync] Using Kitsu server: {parent.kitsu_server_url!r}"
        )
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
        # Create lookup dict for efficient person lookup by ID
        persons_by_id = {person["id"]: person for person in persons}
    except Exception as e:
        logging.error(f"[fullsync] Failed to get persons: {e}")
        log_traceback("Error getting persons")
        raise

    try:
        # Get AYON users once for all tasks
        ayon_users = ayon_api.get_users()
        ayon_users_by_email = {
            user["attrib"]["email"]: user["name"] for user in ayon_users
        }
        logging.debug(
            f"[fullsync] Retrieved {len(ayon_users_by_email)} AYON users"
        )
    except Exception as e:
        logging.error(f"[fullsync] Failed to get AYON users: {e}")
        log_traceback("Error getting AYON users")
        raise

    try:
        assets = get_assets(kitsu_project_id, asset_types)
        logging.info(f"[fullsync] Retrieved {len(assets)} assets")
    except Exception as e:
        logging.error(f"[fullsync] Failed to get assets: {e}")
        log_traceback("Error getting assets")
        raise

    try:
        logging.debug(f"[fullsync] About to call get_tasks...")
        tasks = get_tasks(
            kitsu_project_id,
            task_types,
            task_statuses,
            persons_by_id,
            ayon_users_by_email,
        )
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
    logging.info(
        f"[fullsync] Processing {total_batches} batches of {batch_size} entities each"
    )

    # Track progress and failures
    processed_count = 0
    failed_batches = []
    kitsu_folder_map: dict[str, str] = {}

    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min((batch_num + 1) * batch_size, len(entities))
        batch = entities[start_idx:end_idx]

        logging.info(
            f"[fullsync] Sending batch {batch_num + 1}/{total_batches} ({len(batch)} entities)"
        )

        # Log entity types in this batch for debugging
        entity_types = {}
        for entity in batch:
            entity_type = entity.get("type", "unknown")
            entity_types[entity_type] = entity_types.get(entity_type, 0) + 1
        logging.debug(
            f"[fullsync] Batch {batch_num + 1} contains: {entity_types}"
        )

        try:
            response = ayon_api.post(
                f"{parent.entrypoint}/push",
                project_name=project_name,
                entities=batch,
            )
            response.raise_for_status()
            merge_push_response_folder_map(kitsu_folder_map, response.data)
            processed_count += len(batch)
            logging.info(
                f"[fullsync] Batch {batch_num + 1} processed successfully"
            )
        except Exception as e:
            logging.error(
                f"[fullsync] Failed to push batch {batch_num + 1} for project {project_name}: {e}"
            )
            logging.error(f"[fullsync] Failed batch contained: {entity_types}")
            # Log first few entity IDs for debugging
            entity_ids = [
                f"{ent.get('type', '?')}:{ent.get('id', '?')[:8]}"
                for ent in batch[:5]
            ]
            logging.error(
                f"[fullsync] First entities in failed batch: {entity_ids}"
            )
            log_traceback(
                f"Error pushing batch {batch_num + 1} for {project_name}"
            )
            emit_sync_entity_failed(
                project_name,
                f"Kitsu fullsync batch {batch_num + 1}/{total_batches} push failed: {e}",
                {
                    "phase": "batch_push",
                    "batchIndex": batch_num + 1,
                    "batchTotal": total_batches,
                    "entityTypeCounts": entity_types,
                    "firstEntityIds": entity_ids,
                },
                payload=parse_http_error_detail(e),
            )

            # Immediately try processing individually instead of batch retry
            logging.warning(
                f"[fullsync] Processing batch {batch_num + 1} entities individually to isolate problem..."
            )
            individual_success = 0
            individual_failed = 0

            for idx, entity in enumerate(batch):
                entity_type = "unknown"
                entity_id = "unknown"
                entity_name = "unknown"

                try:
                    entity_type = entity.get("type", "unknown")
                    entity_id = entity.get("id", "unknown")
                    entity_data = entity.get("data")
                    entity_name = entity.get("name", "unknown")
                    if entity_name == "unknown" and isinstance(
                        entity_data, dict
                    ):
                        entity_name = entity_data.get("name", "unknown")

                    response = push_entities_with_relink(
                        parent.entrypoint,
                        project_name,
                        [entity],
                        kitsu_folder_map,
                    )
                    individual_success += 1

                    if idx == 0:
                        logging.info(
                            f"[fullsync] Individual entity processing working..."
                        )
                    elif (idx + 1) % 10 == 0:
                        logging.info(
                            f"[fullsync] Processed {idx + 1}/{len(batch)} entities individually ({individual_success} success, {individual_failed} failed)"
                        )

                except Exception as entity_error:
                    individual_failed += 1
                    logging.error(
                        f"[fullsync] PROBLEMATIC ENTITY #{idx + 1}: {entity_type}:{entity_name} (ID: {entity_id})"
                    )
                    logging.error(f"[fullsync] Error: {entity_error}")
                    logging.debug(f"[fullsync] Entity data: {entity}")
                    emit_sync_entity_failed(
                        project_name,
                        f"Kitsu fullsync entity failed after relink retry: {entity_type} {entity_name} ({entity_id})",
                        {
                            "phase": "individual_push",
                            "batchIndex": batch_num + 1,
                            "entityIndexInBatch": idx + 1,
                            "entityType": entity_type,
                            "kitsuEntityId": str(entity_id),
                            "entityName": str(entity_name),
                            "kitsuParentEntityId": str(entity.get("entity_id", "")),
                            "taskTypeName": str(entity.get("task_type_name", "")),
                        },
                        payload=parse_http_error_detail(entity_error),
                    )

            processed_count += individual_success
            logging.info(
                f"[fullsync] Batch {batch_num + 1} individual processing complete: "
                f"{individual_success} succeeded, {individual_failed} failed"
            )

    # Log summary
    logging.info(
        f"[fullsync] Processed {processed_count}/{len(entities)} entities successfully"
    )

    # Final summary
    if processed_count == len(entities):
        logging.info(
            f"[fullsync] Full Sync for project {project_name} "
            f"completed successfully in {time.time() - start_time:.2f}s"
        )
    else:
        failed_n = len(entities) - processed_count
        logging.warning(
            f"[fullsync] Sync for project {project_name} completed with {failed_n} entities failed"
        )
        emit_sync_summary(
            project_name,
            f"Kitsu fullsync finished with {failed_n} of {len(entities)} entities not pushed",
            {
                "phase": "fullsync_complete",
                "processedCount": processed_count,
                "totalEntities": len(entities),
                "failedCount": failed_n,
            },
        )
