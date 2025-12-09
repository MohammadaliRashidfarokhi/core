"""Test GitHub sensor."""

import json

import pytest

from homeassistant.components.github.const import DOMAIN, FALLBACK_UPDATE_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .common import TEST_REPOSITORY

from tests.common import MockConfigEntry, async_fire_time_changed, async_load_fixture
from tests.test_util.aiohttp import AiohttpClientMocker

TEST_SENSOR_ENTITY = "sensor.octocat_hello_world_latest_release"
WORKFLOW_SENSOR_ENTITY = "sensor.octocat_hello_world_workflow_runs"
WORKFLOW_SUMMARY_SENSOR_ENTITY = "sensor.octocat_hello_world_workflow_summary"
WORKFLOW_ACTIVITY_SENSOR_ENTITY = "sensor.octocat_hello_world_workflow_activity"
TRENDING_SENSOR_ENTITY = "sensor.octocat_hello_world_trending_item"
LABEL_SENSOR_ENTITY = "sensor.octocat_hello_world_bug_issues"


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_sensor_updates_with_empty_release_array(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test the sensor updates by default GitHub sensors."""
    state = hass.states.get(TEST_SENSOR_ENTITY)
    assert state.state == "v1.0.0"

    response_json = json.loads(await async_load_fixture(hass, "graphql.json", DOMAIN))
    response_json["data"]["repository"]["release"] = None
    headers = json.loads(await async_load_fixture(hass, "base_headers.json", DOMAIN))

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/events",
        json=[],
        headers=headers,
    )
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/actions/runs",
        json=json.loads(await async_load_fixture(hass, "workflow_runs.json", DOMAIN)),
        headers=headers,
    )
    aioclient_mock.post(
        "https://api.github.com/graphql",
        json=response_json,
        headers=headers,
    )

    coordinator = next(iter(init_integration.runtime_data.values()))
    coordinator._last_trending_fetch = None

    await coordinator.async_request_refresh()
    await hass.async_block_till_done()

    new_state = hass.states.get(TEST_SENSOR_ENTITY)
    assert new_state.state == "unavailable"


async def test_workflow_sensor_attributes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test workflow run sensor exposes the latest status."""
    summary_state = hass.states.get(WORKFLOW_SUMMARY_SENSOR_ENTITY)
    assert summary_state
    assert summary_state.state == "Deploy Backend (Elastic Beanstalk)"
    attributes = summary_state.attributes
    assert attributes["run_id"] == 18952639482
    assert attributes["status"] == "success"
    assert attributes["conclusion"] == "success"
    assert attributes["head_branch"] == "main"
    assert attributes["successful_runs"] == 2
    assert attributes["failed_runs"] == 2
    assert attributes["in_progress_runs"] == 1
    assert attributes["latest_run_url"].startswith("https://github.com/NetologyAB/")
    assert len(attributes["recent_runs"]) == 5
    assert attributes["recent_runs"][2]["status"] == "in_progress"
    assert attributes["recent_runs"][2]["conclusion"] == "unknown"

    activity_state = hass.states.get(WORKFLOW_ACTIVITY_SENSOR_ENTITY)
    assert activity_state
    assert activity_state.state == "success"
    assert activity_state.attributes["icon"] == "mdi:check-circle"
    assert activity_state.attributes["latest_run_url"].startswith(
        "https://github.com/NetologyAB/"
    )

    runs_state = hass.states.get(WORKFLOW_SENSOR_ENTITY)
    assert runs_state.state == "Deploy Backend (Elastic Beanstalk) (success)"


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_workflow_sensor_handles_empty_runs(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-functional (N.WF.02): Ensure missing runs map to unknown without errors."""
    headers = json.loads(await async_load_fixture(hass, "base_headers.json", DOMAIN))
    response_json = json.loads(await async_load_fixture(hass, "graphql.json", DOMAIN))
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/events",
        json=[],
        headers=headers,
    )
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/actions/runs",
        json=json.loads(
            await async_load_fixture(hass, "workflow_runs_empty.json", DOMAIN)
        ),
        headers=headers,
    )
    aioclient_mock.get(
        "https://api.github.com/search/issues",
        params={
            "q": f'repo:{TEST_REPOSITORY} state:open type:issue label:"bug"',
            "per_page": 50,
        },
        json=json.loads(
            await async_load_fixture(hass, "label_search_bug.json", DOMAIN)
        ),
        headers=headers,
    )
    aioclient_mock.post(
        "https://api.github.com/graphql",
        json=response_json,
        headers=headers,
    )

    async_fire_time_changed(hass, dt_util.utcnow() + FALLBACK_UPDATE_INTERVAL)
    await hass.async_block_till_done()

    state = hass.states.get(WORKFLOW_SENSOR_ENTITY)
    assert state.state == "No Workflow Activity"
    attributes = state.attributes
    assert attributes["status"] == "unknown"
    assert attributes["conclusion"] == "unknown"
    assert attributes["latest_run_url"] == "unknown"
    assert attributes["run_id"] == "unknown"
    assert attributes["recent_runs"] == []

    summary_state = hass.states.get(WORKFLOW_SUMMARY_SENSOR_ENTITY)
    assert summary_state.state == "No Workflow Activity"

    activity_state = hass.states.get(WORKFLOW_ACTIVITY_SENSOR_ENTITY)
    assert activity_state.state == "unknown"
    assert activity_state.attributes["icon"] == "mdi:progress-question"


async def test_workflow_sensor_handles_missing_fields(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-functional (N.WF.02): Gracefully handle null fields as unknown."""
    headers = json.loads(await async_load_fixture(hass, "base_headers.json", DOMAIN))
    response_json = json.loads(await async_load_fixture(hass, "graphql.json", DOMAIN))
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/events",
        json=[],
        headers=headers,
    )
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/actions/runs",
        json=json.loads(
            await async_load_fixture(hass, "workflow_runs_missing_fields.json", DOMAIN)
        ),
        headers=headers,
    )
    aioclient_mock.get(
        "https://api.github.com/search/issues",
        params={
            "q": f'repo:{TEST_REPOSITORY} state:open type:issue label:"bug"',
            "per_page": 50,
        },
        json=json.loads(
            await async_load_fixture(hass, "label_search_bug.json", DOMAIN)
        ),
        headers=headers,
    )
    aioclient_mock.post(
        "https://api.github.com/graphql",
        json=response_json,
        headers=headers,
    )

    async_fire_time_changed(hass, dt_util.utcnow() + FALLBACK_UPDATE_INTERVAL)
    await hass.async_block_till_done()

    summary_state = hass.states.get(WORKFLOW_SUMMARY_SENSOR_ENTITY)
    assert summary_state.state == "Unknown Workflow"
    activity_state = hass.states.get(WORKFLOW_ACTIVITY_SENSOR_ENTITY)
    assert activity_state.state == "unknown"
    attributes = activity_state.attributes
    assert attributes["head_branch"] == "unknown"
    assert attributes["run_started_at"] == "unknown"
    assert attributes["latest_run_url"] == "unknown"
    assert attributes["run_id"] == "unknown"
    assert attributes["display_title"] == "Unknown Workflow"


async def test_trending_sensor(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test trending sensor shows the most active issue/discussion."""
    state = hass.states.get(TRENDING_SENSOR_ENTITY)
    assert state
    assert state.state == "Found a bug"
    attributes = state.attributes
    assert attributes["item_type"] == "issue"
    assert attributes["activity_score"] == 10
    assert attributes["creation_date"] == "2025-12-01T00:00:00Z"
    assert attributes["lookback_days"] == 7
    assert attributes["url"].endswith("/issues/1347")


async def test_trending_sensor_handles_empty_results(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-functional (N.TDI.03/N.TDI.04): Fallback when nothing is trending."""
    headers = json.loads(await async_load_fixture(hass, "base_headers.json", DOMAIN))
    response_json = json.loads(await async_load_fixture(hass, "graphql.json", DOMAIN))
    response_json["data"]["trending_issue_search"]["nodes"] = []
    response_json["data"]["repository"]["trending_discussions"]["nodes"] = []
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/events",
        json=[],
        headers=headers,
    )
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/actions/runs",
        json=json.loads(await async_load_fixture(hass, "workflow_runs.json", DOMAIN)),
        headers=headers,
    )
    aioclient_mock.post(
        "https://api.github.com/graphql",
        json=response_json,
        headers=headers,
    )
    aioclient_mock.get(
        "https://api.github.com/search/issues",
        params={
            "q": f'repo:{TEST_REPOSITORY} state:open type:issue label:"bug"',
            "per_page": 50,
        },
        json=json.loads(
            await async_load_fixture(hass, "label_search_bug.json", DOMAIN)
        ),
        headers=headers,
    )

    coordinator = next(iter(init_integration.runtime_data.values()))
    coordinator._last_trending_fetch = None
    await coordinator.async_request_refresh()

    state = hass.states.get(TRENDING_SENSOR_ENTITY)
    assert state.state == "No Trending Activity"
    attributes = state.attributes
    assert attributes["activity_score"] == 0
    assert attributes["url"] is None


async def test_workflow_runs_cached_on_etag(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-functional (N.WF.04): Use cached runs when server replies 304."""
    headers = json.loads(await async_load_fixture(hass, "base_headers.json", DOMAIN))
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/events",
        json=[],
        headers=headers,
    )
    aioclient_mock.get(
        f"https://api.github.com/repos/{TEST_REPOSITORY}/actions/runs",
        status=304,
        headers=headers,
    )
    aioclient_mock.get(
        "https://api.github.com/search/issues",
        params={
            "q": f'repo:{TEST_REPOSITORY} state:open type:issue label:"bug"',
            "per_page": 50,
        },
        json=json.loads(
            await async_load_fixture(hass, "label_search_bug.json", DOMAIN)
        ),
        headers=headers,
    )
    aioclient_mock.post(
        "https://api.github.com/graphql",
        json=json.loads(await async_load_fixture(hass, "graphql.json", DOMAIN)),
        headers=headers,
    )

    previous_state = hass.states.get(WORKFLOW_SENSOR_ENTITY)
    async_fire_time_changed(hass, dt_util.utcnow() + FALLBACK_UPDATE_INTERVAL)
    await hass.async_block_till_done()

    state = hass.states.get(WORKFLOW_SENSOR_ENTITY)
    assert state == previous_state
    assert state.attributes["recent_runs"]


async def test_label_issue_sensor(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test label issue sensor shows counts and issue list."""
    state = hass.states.get(LABEL_SENSOR_ENTITY)
    assert state
    assert state.state == "2"
    attributes = state.attributes
    assert attributes["label"] == "bug"
    assert attributes["issues"][0]["number"] == 123
    assert attributes["issues"][0]["url"].endswith("/issues/123")
    assert attributes["last_checked"]
