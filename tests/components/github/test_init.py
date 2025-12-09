"""Test the GitHub init file."""

import pytest

from homeassistant.components.github import CONF_REPOSITORIES
from homeassistant.components.github.const import (
    CONF_TRENDING_LOOKBACK_DAYS,
    CONF_WORKFLOW_POLLING_INTERVAL,
    DEFAULT_TRENDING_LOOKBACK_DAYS,
    DEFAULT_WORKFLOW_POLLING_INTERVAL_MINUTES,
    FALLBACK_UPDATE_INTERVAL,
    FAST_UPDATE_INTERVAL,
    FAST_WORKFLOW_POLLING_INTERVAL_MINUTES,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, icon

from .common import setup_github_integration

from tests.common import MockConfigEntry
from tests.test_util.aiohttp import AiohttpClientMocker


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_device_registry_cleanup(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that we remove untracked repositories from the device registry."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_REPOSITORIES: ["home-assistant/core"],
            CONF_WORKFLOW_POLLING_INTERVAL: DEFAULT_WORKFLOW_POLLING_INTERVAL_MINUTES,
            CONF_TRENDING_LOOKBACK_DAYS: DEFAULT_TRENDING_LOOKBACK_DAYS,
        },
    )
    await setup_github_integration(
        hass, mock_config_entry, aioclient_mock, add_entry_to_hass=False
    )

    devices = dr.async_entries_for_config_entry(
        registry=device_registry,
        config_entry_id=mock_config_entry.entry_id,
    )

    assert len(devices) == 1

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_REPOSITORIES: [],
            CONF_WORKFLOW_POLLING_INTERVAL: DEFAULT_WORKFLOW_POLLING_INTERVAL_MINUTES,
            CONF_TRENDING_LOOKBACK_DAYS: DEFAULT_TRENDING_LOOKBACK_DAYS,
        },
    )
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        f"Unlinking device {devices[0].id} for untracked repository home-assistant/core from config entry {mock_config_entry.entry_id}"
        in caplog.text
    )

    devices = dr.async_entries_for_config_entry(
        registry=device_registry,
        config_entry_id=mock_config_entry.entry_id,
    )

    assert len(devices) == 0


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_subscription_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test that we setup event subscription."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_REPOSITORIES: ["home-assistant/core"],
            CONF_WORKFLOW_POLLING_INTERVAL: DEFAULT_WORKFLOW_POLLING_INTERVAL_MINUTES,
            CONF_TRENDING_LOOKBACK_DAYS: DEFAULT_TRENDING_LOOKBACK_DAYS,
        },
        pref_disable_polling=False,
    )
    await setup_github_integration(
        hass, mock_config_entry, aioclient_mock, add_entry_to_hass=False
    )
    assert (
        "https://api.github.com/repos/home-assistant/core/events" in x[1]
        for x in aioclient_mock.mock_calls
    )


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_subscription_setup_polling_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test that we do not setup event subscription if polling is disabled."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_REPOSITORIES: ["home-assistant/core"],
            CONF_WORKFLOW_POLLING_INTERVAL: DEFAULT_WORKFLOW_POLLING_INTERVAL_MINUTES,
            CONF_TRENDING_LOOKBACK_DAYS: DEFAULT_TRENDING_LOOKBACK_DAYS,
        },
        pref_disable_polling=True,
    )
    await setup_github_integration(
        hass, mock_config_entry, aioclient_mock, add_entry_to_hass=False
    )
    assert (
        "https://api.github.com/repos/home-assistant/core/events" not in x[1]
        for x in aioclient_mock.mock_calls
    )

    # Prove that we subscribed if the user enabled polling again
    hass.config_entries.async_update_entry(
        mock_config_entry, pref_disable_polling=False
    )
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        "https://api.github.com/repos/home-assistant/core/events" in x[1]
        for x in aioclient_mock.mock_calls
    )


# This tests needs to be adjusted to remove lingering tasks
@pytest.mark.parametrize("expected_lingering_tasks", [True])
async def test_sensor_icons(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test to ensure that all sensor entities have an icon definition."""
    entities = er.async_entries_for_config_entry(
        entity_registry,
        config_entry_id=init_integration.entry_id,
    )

    icons = await icon.async_get_icons(hass, "entity", integrations=["github"])
    for entity in entities:
        if entity.translation_key is None:
            continue
        assert icons["github"]["sensor"][entity.translation_key] is not None


async def test_workflow_polling_interval_defaults(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-functional (N.WF.01): Default interval uses 15 minutes."""
    await setup_github_integration(hass, mock_config_entry, aioclient_mock)
    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    assert coordinator.update_interval == FALLBACK_UPDATE_INTERVAL


async def test_workflow_polling_interval_fast_option(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-functional (N.WF.01): Selecting fast interval uses 5 minutes."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            **mock_config_entry.options,
            CONF_WORKFLOW_POLLING_INTERVAL: FAST_WORKFLOW_POLLING_INTERVAL_MINUTES,
        },
    )
    await setup_github_integration(
        hass, mock_config_entry, aioclient_mock, add_entry_to_hass=False
    )
    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    assert coordinator.update_interval == FAST_UPDATE_INTERVAL
