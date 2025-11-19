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
        json=json.loads(
            await async_load_fixture(hass, "workflow_runs.json", DOMAIN)
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

    new_state = hass.states.get(TEST_SENSOR_ENTITY)
    assert new_state.state == "unavailable"


async def test_workflow_sensor_attributes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test workflow run sensor exposes the latest status."""
    state = hass.states.get(WORKFLOW_SENSOR_ENTITY)
    assert state
    assert state.state == "Deploy Backend (Elastic Beanstalk) (success)"
    attributes = state.attributes
    assert attributes["run_id"] == 18952639482
    assert attributes["status"] == "success"
    assert attributes["head_branch"] == "main"
    assert attributes["successful_runs"] == 2
    assert attributes["failed_runs"] == 2
    assert attributes["in_progress_runs"] == 1
    assert attributes["latest_run_url"].startswith("https://github.com/NetologyAB/")
    assert len(attributes["recent_runs"]) == 5
    assert attributes["recent_runs"][2]["status"] == "in_progress"
    assert attributes["icon"] == "mdi:check-circle"


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_workflow_sensor_handles_empty_runs(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test workflow run sensor handles missing runs."""
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
    assert attributes["recent_runs"] == []
