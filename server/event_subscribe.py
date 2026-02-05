# -*- coding: utf-8 -*-
"""
Server-side AYON event subscriptions.

Register all addon event handlers here. Add new topic/handler pairs to
SUBSCRIPTIONS; they are registered in register_event_subscriptions(addon).
"""

from nxtools import logging

from ayon_server.events import EventStream


# Topic -> addon method name (must exist on addon class).
# Add new subscriptions here.
# entity.task.status_changed: dispatched when our push/sync updates task status.
# entity.task.data_changed: dispatched when core updates task (e.g. UI status change).
SUBSCRIPTIONS = [
    (
        "entity.task.status_changed",
        "on_task_status_changed",
        "uniqueSprites bubble-up (status_changed)",
    ),
    (
        "entity.task.data_changed",
        "on_task_data_changed",
        "uniqueSprites bubble-up (data_changed / UI)",
    ),
]


def register_event_subscriptions(addon) -> None:
    """Subscribe the addon to AYON server events. Call from addon.initialize()."""
    for topic, handler_name, description in SUBSCRIPTIONS:
        handler = getattr(addon, handler_name, None)
        if handler is None:
            logging.warning(
                f"[ayon-kitsu][server] Event handler {handler_name!r} not found on addon, "
                f"skipping subscription to {topic!r}"
            )
            continue
        EventStream.subscribe(topic, handler)
        logging.info(
            f"[ayon-kitsu][server] Subscribed to {topic!r} ({description})"
        )
