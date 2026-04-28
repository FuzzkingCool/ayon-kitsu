# Concept sync: migrating to `per_linked_entity`

When **`sync_settings.concept_sync.concept_entity_model`** is set to **`per_linked_entity`**, AYON Concept folders use **`data.kitsuId` = the linked Kitsu entity id** (e.g. asset), not the Kitsu concept row id. Several Kitsu concepts that link the same asset converge on one folder.

## Legacy state

Older syncs stored **`data.kitsuId`** as the **Kitsu concept** uuid. After enabling `per_linked_entity`, the processor sends expanded payloads keyed by the linked entity id.

The processor expands Kitsu concepts to **one push row per linked entity id** (not per ``(link, parent_id)`` pair), so inconsistent ``parent_id`` values across duplicate concept rows cannot emit duplicate creates that collide on AYON ``(parent_id, folder.name)``.

## Automatic repair on push

On **`POST /push`**, when a linked-entity payload finds no folder by the new id, the server runs a one-shot match: under the same AYON parent, a Concept folder whose **`data.kitsuId`** equals any id in **`kitsuSourceConceptIds`** on the payload is **retargeted** to the linked entity id and contributor ids are merged into **`data.kitsuSourceConceptIds`**.

If more than one legacy folder matches, migration is skipped (logged) to avoid ambiguous updates.

## VizDev tasks

- Legacy surrogate: `kitsu:concept:{concept_uuid}:vizdev`
- Linked mode surrogate: `kitsu:link:{linked_entity_uuid}:vizdev`

After migration, the next **`ensure_concept_vizdev_task`** pass adopts or creates the task with the link surrogate. Old surrogate tasks on the same folder are updated when the task is matched by surrogate id.

## Operator checklist

1. Enable **`per_linked_entity`** in studio sync settings (and mirror the same flags in the processor service settings JSON if you manage it separately).
2. Run a **project fullsync** (or re-push concepts) so folders and VizDev tasks align.
3. Verify dashboards or tools that assumed **`folder.data.kitsuId` == Kitsu concept id**; they must use **`kitsuSourceConceptIds`** or resolve via Kitsu APIs when needed.
