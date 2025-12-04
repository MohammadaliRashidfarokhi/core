"""Constants for the GitHub integration."""

from __future__ import annotations

from datetime import timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "github"

CLIENT_ID = "1440cafcc86e3ea5d6a2"

DEFAULT_REPOSITORIES = ["home-assistant/core", "esphome/esphome"]
FALLBACK_UPDATE_INTERVAL = timedelta(minutes=15)
FAST_UPDATE_INTERVAL = timedelta(minutes=5)

CONF_REPOSITORIES = "repositories"
CONF_WORKFLOW_POLLING_INTERVAL = "workflow_polling_interval"
DEFAULT_WORKFLOW_POLLING_INTERVAL_MINUTES = 15
FAST_WORKFLOW_POLLING_INTERVAL_MINUTES = 5


REFRESH_EVENT_TYPES = (
    "CreateEvent",
    "ForkEvent",
    "IssuesEvent",
    "PullRequestEvent",
    "PushEvent",
    "ReleaseEvent",
    "WatchEvent",
)
