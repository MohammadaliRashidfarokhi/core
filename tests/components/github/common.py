"""Common helpers for GitHub integration tests."""

from __future__ import annotations

import json

from homeassistant.components.github.const import CONF_REPOSITORIES, DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry, async_load_fixture
from tests.test_util.aiohttp import AiohttpClientMocker

MOCK_ACCESS_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
TEST_REPOSITORY = "octocat/Hello-World"


async def setup_github_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    add_entry_to_hass: bool = True,
) -> None:
    """Mock setting up the integration."""
    headers = json.loads(await async_load_fixture(hass, "base_headers.json", DOMAIN))
    workflow_runs = json.loads(
        await async_load_fixture(hass, "workflow_runs.json", DOMAIN)
    )
    label_search = json.loads(
        await async_load_fixture(hass, "label_search_bug.json", DOMAIN)
    )
    for idx, repository in enumerate(mock_config_entry.options[CONF_REPOSITORIES]):
        aioclient_mock.get(
            f"https://api.github.com/repos/{repository}",
            json={
                **json.loads(await async_load_fixture(hass, "repository.json", DOMAIN)),
                "full_name": repository,
                "id": idx,
            },
            headers=headers,
        )
        aioclient_mock.get(
            f"https://api.github.com/repos/{repository}/events",
            json=[],
            headers=headers,
        )
        aioclient_mock.get(
            "https://api.github.com/search/issues",
            params={
                "q": f'repo:{repository} state:open type:issue label:"bug"',
                "per_page": 50,
            },
            json=label_search,
            headers=headers,
        )
        aioclient_mock.get(
            f"https://api.github.com/repos/{repository}/actions/runs",
            json=workflow_runs,
            headers=headers,
        )
    aioclient_mock.post(
        "https://api.github.com/graphql",
        json=json.loads(await async_load_fixture(hass, "graphql.json", DOMAIN)),
        headers=headers,
    )
    if add_entry_to_hass:
        mock_config_entry.add_to_hass(hass)

    setup_result = await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert setup_result
    assert mock_config_entry.state is ConfigEntryState.LOADED
