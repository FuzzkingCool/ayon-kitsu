# -*- coding: utf-8 -*-
"""Shared literals for sync settings UI and checklist ↔ AYON status mapping."""

# Labels for default_sync_info status ``state`` enum (sync_settings._states_enum).
SYNC_STATE_LABEL_NOT_STARTED = "Ready"
SYNC_STATE_LABEL_IN_PROGRESS = "Work In progress"
SYNC_STATE_LABEL_DONE = "Done"
SYNC_STATE_LABEL_BLOCKED = "Hold"

# Default AYON task status names for Kitsu checklist child tasks (checklist_subtasks).
DEFAULT_CHECKLIST_DONE_STATUS_NAME = SYNC_STATE_LABEL_DONE
DEFAULT_CHECKLIST_WIP_STATUS_NAME = "Work In Progress"
