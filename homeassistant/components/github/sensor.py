"""Sensor platform for the GitHub integration, including issue dashboard, trending activity, and workflow views."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ISSUE_LABELS, DOMAIN
from .coordinator import GithubConfigEntry, GitHubDataUpdateCoordinator

WORKFLOW_STATUS_SUCCESS = "success"
WORKFLOW_STATUS_FAILURE = "failure"
WORKFLOW_STATUS_IN_PROGRESS = "in_progress"
WORKFLOW_STATUS_UNKNOWN = "unknown"
WORKFLOW_NO_ACTIVITY = "No Workflow Activity"
TRENDING_NO_ACTIVITY = "No Trending Activity"
WORKFLOW_STATUS_PROGRESS_STATES = {"queued", "in_progress", "pending", "waiting"}
WORKFLOW_STATUS_FAILURE_STATES = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
    "neutral",
    "skipped",
}
WORKFLOW_ICON_MAP = {
    WORKFLOW_STATUS_SUCCESS: "mdi:check-circle",
    WORKFLOW_STATUS_FAILURE: "mdi:alert-circle",
    WORKFLOW_STATUS_IN_PROGRESS: "mdi:progress-clock",
    "skipped": "mdi:skip-forward",
}
TRENDING_ICON_ACTIVE = "mdi:fire"
TRENDING_ICON_INACTIVE = "mdi:fire-off"


@dataclass(frozen=True, kw_only=True)
class GitHubSensorEntityDescription(SensorEntityDescription):
    """Describes GitHub issue sensor entity."""

    value_fn: Callable[[dict[str, Any]], StateType]

    attr_fn: Callable[[dict[str, Any]], Mapping[str, Any] | None] = lambda data: None
    avabl_fn: Callable[[dict[str, Any]], bool] = lambda data: True


def _workflow_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return workflow run payload from coordinator data."""
    workflow_data = data.get("workflow_runs")
    if isinstance(workflow_data, dict):
        return workflow_data
    return {"recent_runs": []}


def _workflow_runs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cached workflow runs."""
    workflow_data = _workflow_data(data)
    runs = workflow_data.get("recent_runs")
    if isinstance(runs, list):
        return runs
    return []


def _latest_workflow_run(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most recent workflow run."""
    runs = _workflow_runs(data)
    if runs:
        return runs[0]
    return None


def _workflow_summary_value(data: dict[str, Any]) -> str:
    """Return the display value for the workflow summary sensor used in the workflow activity view."""
    latest_run = _latest_workflow_run(data)
    if not latest_run:
        return WORKFLOW_NO_ACTIVITY
    return _workflow_display_title(latest_run)


def _workflow_display_title(run: dict[str, Any]) -> str:
    """Return a printable workflow title."""
    for key in ("display_title", "name"):
        if title := run.get(key):
            return str(title)[:255]
    return WORKFLOW_STATUS_UNKNOWN


def _normalize_workflow_status(run: dict[str, Any]) -> str:
    """Normalize the workflow run status string."""
    status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    if status in WORKFLOW_STATUS_PROGRESS_STATES:
        return WORKFLOW_STATUS_IN_PROGRESS
    if status and status != "completed":
        return status
    if conclusion == WORKFLOW_STATUS_SUCCESS:
        return WORKFLOW_STATUS_SUCCESS
    if conclusion in WORKFLOW_STATUS_FAILURE_STATES:
        return WORKFLOW_STATUS_FAILURE
    if conclusion:
        return conclusion
    return WORKFLOW_STATUS_UNKNOWN


def _workflow_state_value(data: dict[str, Any]) -> str:
    """Return the workflow runs sensor state showing the latest title plus status."""
    latest_run = _latest_workflow_run(data)
    if not latest_run:
        return WORKFLOW_NO_ACTIVITY
    status = _normalize_workflow_status(latest_run)
    return f"{_workflow_display_title(latest_run)} ({status})"


def _workflow_activity_value(data: dict[str, Any]) -> str:
    """Return the normalized status string used by the workflow activity sensor."""
    latest_run = _latest_workflow_run(data)
    if not latest_run:
        return WORKFLOW_STATUS_UNKNOWN
    return _normalize_workflow_status(latest_run)


def _serialize_workflow_run(run: dict[str, Any]) -> dict[str, Any]:
    """Serialize a workflow run entry."""
    run_id = run.get("id")
    conclusion = run.get("conclusion")
    return {
        "run_id": run_id if run_id is not None else WORKFLOW_STATUS_UNKNOWN,
        "display_title": _workflow_display_title(run),
        "status": _normalize_workflow_status(run),
        "conclusion": conclusion or WORKFLOW_STATUS_UNKNOWN,
        "head_branch": run.get("head_branch") or WORKFLOW_STATUS_UNKNOWN,
        "run_started_at": run.get("run_started_at") or WORKFLOW_STATUS_UNKNOWN,
        "html_url": run.get("html_url") or WORKFLOW_STATUS_UNKNOWN,
    }


def _workflow_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    """Return aggregate counts for workflow runs."""
    counts = {
        "successful_runs": 0,
        "failed_runs": 0,
        "in_progress_runs": 0,
    }
    for run in runs:
        status = _normalize_workflow_status(run)
        if status == WORKFLOW_STATUS_SUCCESS:
            counts["successful_runs"] += 1
        elif status == WORKFLOW_STATUS_IN_PROGRESS:
            counts["in_progress_runs"] += 1
        else:
            counts["failed_runs"] += 1
    return counts


def _workflow_attributes(data: dict[str, Any]) -> Mapping[str, Any]:
    """Return workflow sensor attributes that power the workflow activity view."""
    runs = _workflow_runs(data)
    latest_run = runs[0] if runs else None
    attributes: dict[str, Any] = {
        **_workflow_counts(runs),
        "recent_runs": [_serialize_workflow_run(run) for run in runs],
    }
    if latest_run:
        conclusion = latest_run.get("conclusion")
        run_id = latest_run.get("id")
        attributes.update(
            {
                "run_id": run_id if run_id is not None else WORKFLOW_STATUS_UNKNOWN,
                "display_title": _workflow_display_title(latest_run),
                "head_branch": latest_run.get("head_branch") or WORKFLOW_STATUS_UNKNOWN,
                "status": _normalize_workflow_status(latest_run),
                "conclusion": conclusion or WORKFLOW_STATUS_UNKNOWN,
                "run_started_at": latest_run.get("run_started_at")
                or WORKFLOW_STATUS_UNKNOWN,
                "latest_run_url": latest_run.get("html_url") or WORKFLOW_STATUS_UNKNOWN,
            }
        )
    else:
        attributes.update(
            {
                "status": WORKFLOW_STATUS_UNKNOWN,
                "head_branch": WORKFLOW_STATUS_UNKNOWN,
                "conclusion": WORKFLOW_STATUS_UNKNOWN,
                "run_started_at": WORKFLOW_STATUS_UNKNOWN,
                "latest_run_url": WORKFLOW_STATUS_UNKNOWN,
                "run_id": WORKFLOW_STATUS_UNKNOWN,
                "display_title": WORKFLOW_STATUS_UNKNOWN,
            }
        )
    return attributes


def _workflow_status(data: dict[str, Any]) -> str:
    """Return the normalized status for the latest workflow run."""
    latest_run = _latest_workflow_run(data)
    if not latest_run:
        return WORKFLOW_STATUS_UNKNOWN
    return _normalize_workflow_status(latest_run)


def _trending_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return the cached trending issue/discussion payload from the coordinator data."""
    trending = data.get("trending")
    if isinstance(trending, dict):
        return trending
    return {}


def _trending_state_value(data: dict[str, Any]) -> str:
    """Return the title shown by the trending issue/discussion sensor."""
    trending = _trending_payload(data)
    if not trending.get("title"):
        return TRENDING_NO_ACTIVITY
    return str(trending["title"])[:255]


def _trending_attributes(data: dict[str, Any]) -> Mapping[str, Any]:
    """Return metadata attributes for the trending activity sensor."""
    trending = _trending_payload(data)
    return {
        "url": trending.get("url"),
        "item_type": trending.get("item_type"),
        "activity_score": trending.get("activity_score"),
        "creation_date": trending.get("creation_date"),
        "lookback_days": trending.get("lookback_days"),
    }


def _label_slug(label: str) -> str:
    """Return slugified label used for unique IDs on the per-label issue dashboard."""
    return re.sub(r"[^a-z0-9_]", "_", label.lower())


def _label_issue_data(data: dict[str, Any], label: str) -> dict[str, Any] | None:
    """Return the issue payload stored for a specific label to support the issue dashboard."""
    labels = data.get("label_issues")
    if isinstance(labels, dict):
        return labels.get(label.lower())
    return None


SENSOR_DESCRIPTIONS: tuple[GitHubSensorEntityDescription, ...] = (
    GitHubSensorEntityDescription(
        key="discussions_count",
        translation_key="discussions_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["discussion"]["total"],
    ),
    GitHubSensorEntityDescription(
        key="stargazers_count",
        translation_key="stargazers_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["stargazers_count"],
    ),
    GitHubSensorEntityDescription(
        key="subscribers_count",
        translation_key="subscribers_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["watchers"]["total"],
    ),
    GitHubSensorEntityDescription(
        key="forks_count",
        translation_key="forks_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["forks_count"],
    ),
    GitHubSensorEntityDescription(
        key="issues_count",
        translation_key="issues_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["issue"]["total"],
    ),
    GitHubSensorEntityDescription(
        key="pulls_count",
        translation_key="pulls_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data["pull_request"]["total"],
    ),
    GitHubSensorEntityDescription(
        key="latest_commit",
        translation_key="latest_commit",
        value_fn=lambda data: data["default_branch_ref"]["commit"]["message"][:255],
        attr_fn=lambda data: {
            "sha": data["default_branch_ref"]["commit"]["sha"],
            "url": data["default_branch_ref"]["commit"]["url"],
        },
    ),
    GitHubSensorEntityDescription(
        key="latest_discussion",
        translation_key="latest_discussion",
        avabl_fn=lambda data: data["discussion"]["discussions"],
        value_fn=lambda data: data["discussion"]["discussions"][0]["title"][:255],
        attr_fn=lambda data: {
            "url": data["discussion"]["discussions"][0]["url"],
            "number": data["discussion"]["discussions"][0]["number"],
        },
    ),
    GitHubSensorEntityDescription(
        key="latest_release",
        translation_key="latest_release",
        avabl_fn=lambda data: data["release"] is not None,
        value_fn=lambda data: data["release"]["name"][:255],
        attr_fn=lambda data: {
            "url": data["release"]["url"],
            "tag": data["release"]["tag"],
        },
    ),
    GitHubSensorEntityDescription(
        key="latest_issue",
        translation_key="latest_issue",
        avabl_fn=lambda data: data["issue"]["issues"],
        value_fn=lambda data: data["issue"]["issues"][0]["title"][:255],
        attr_fn=lambda data: {
            "url": data["issue"]["issues"][0]["url"],
            "number": data["issue"]["issues"][0]["number"],
        },
    ),
    GitHubSensorEntityDescription(
        key="latest_pull_request",
        translation_key="latest_pull_request",
        avabl_fn=lambda data: data["pull_request"]["pull_requests"],
        value_fn=lambda data: data["pull_request"]["pull_requests"][0]["title"][:255],
        attr_fn=lambda data: {
            "url": data["pull_request"]["pull_requests"][0]["url"],
            "number": data["pull_request"]["pull_requests"][0]["number"],
        },
    ),
    GitHubSensorEntityDescription(
        key="latest_tag",
        translation_key="latest_tag",
        avabl_fn=lambda data: data["refs"]["tags"],
        value_fn=lambda data: data["refs"]["tags"][0]["name"][:255],
        attr_fn=lambda data: {
            "url": data["refs"]["tags"][0]["target"]["url"],
        },
    ),
    GitHubSensorEntityDescription(
        key="trending_item",
        translation_key="trending_item",
        name="Trending item",
        value_fn=_trending_state_value,
        attr_fn=_trending_attributes,
    ),
    GitHubSensorEntityDescription(
        key="workflow_runs",
        translation_key="workflow_runs",
        name="Workflow runs",
        value_fn=_workflow_state_value,
        attr_fn=_workflow_attributes,
    ),
    GitHubSensorEntityDescription(
        key="workflow_summary",
        translation_key="workflow_summary",
        name="Workflow summary",
        value_fn=_workflow_summary_value,
        attr_fn=_workflow_attributes,
    ),
    GitHubSensorEntityDescription(
        key="workflow_activity",
        translation_key="workflow_activity",
        name="Workflow activity",
        value_fn=_workflow_activity_value,
        attr_fn=_workflow_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GithubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up GitHub sensors (standard, workflow, trending, and label dashboards) from a config entry."""
    repositories = entry.runtime_data
    labels: list[str] = entry.options.get(CONF_ISSUE_LABELS, [])
    entities: list[SensorEntity] = []
    for coordinator in repositories.values():
        entities.extend(
            GitHubSensorEntity(coordinator, description)
            for description in SENSOR_DESCRIPTIONS
        )
        entities.extend(GitHubLabelIssueSensor(coordinator, label) for label in labels)

    async_add_entities(entities)


class GitHubSensorEntity(CoordinatorEntity[GitHubDataUpdateCoordinator], SensorEntity):
    """Defines a GitHub sensor entity for trending and workflow data."""

    _attr_attribution = "Data provided by the GitHub API"
    _attr_has_entity_name = True

    entity_description: GitHubSensorEntityDescription

    def __init__(
        self,
        coordinator: GitHubDataUpdateCoordinator,
        entity_description: GitHubSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator=coordinator)

        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.data.get('id')}_{entity_description.key}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.repository)},
            name=coordinator.data.get("full_name"),
            manufacturer="GitHub",
            configuration_url=f"https://github.com/{coordinator.repository}",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            super().available
            and self.coordinator.data is not None
            and self.entity_description.avabl_fn(self.coordinator.data)
        )

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the extra state attributes."""
        return self.entity_description.attr_fn(self.coordinator.data)

    @property
    def icon(self) -> str | None:
        """Return a dynamic icon for the trending item and workflow sensors."""
        if self.entity_description.key == "trending_item":
            state = _trending_state_value(self.coordinator.data)
            return (
                TRENDING_ICON_ACTIVE
                if state != TRENDING_NO_ACTIVITY
                else TRENDING_ICON_INACTIVE
            )
        if self.entity_description.key not in {
            "workflow_runs",
            "workflow_activity",
        }:
            return super().icon
        status = _workflow_status(self.coordinator.data)
        return WORKFLOW_ICON_MAP.get(status, "mdi:progress-question")


class GitHubLabelIssueSensor(
    CoordinatorEntity[GitHubDataUpdateCoordinator], SensorEntity
):
    """Sensor that powers the per-label issue dashboard."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:tag-multiple"

    def __init__(self, coordinator: GitHubDataUpdateCoordinator, label: str) -> None:
        """Initialize label issue sensor used by the issue dashboard."""
        super().__init__(coordinator=coordinator)
        self._label = label
        slug = _label_slug(label)
        repo = coordinator.repository
        self._attr_unique_id = f"{coordinator.data.get('id')}_label_{slug}"
        self._attr_name = f"{label} issues"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, repo)},
            name=coordinator.data.get("full_name"),
            manufacturer="GitHub",
            configuration_url=f"https://github.com/{repo}",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Return True when data for the configured label is available."""
        return (
            super().available
            and _label_issue_data(self.coordinator.data, self._label) is not None
        )

    @property
    def native_value(self) -> StateType:
        """Return the open issue count used on the issue dashboard."""
        data = _label_issue_data(self.coordinator.data, self._label)
        if not data:
            return None
        return data.get("count")

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the issue list and metadata displayed on the issue dashboard."""
        data = _label_issue_data(self.coordinator.data, self._label)
        if not data:
            return None
        return {
            "issues": data.get("issues", []),
            "last_checked": data.get("last_checked"),
            "label": self._label,
        }
