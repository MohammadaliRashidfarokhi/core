"""Tests for GitHub coordinator."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from aiogithubapi import (
    GitHubConnectionException,
    GitHubException,
    GitHubRatelimitException,
)
from aiohttp import ClientError
import pytest

from homeassistant.components.github.const import REFRESH_EVENT_TYPES
from homeassistant.components.github.coordinator import GitHubDataUpdateCoordinator
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

# --------------------------------------------------------------------
# Async Mock Helpers
# --------------------------------------------------------------------


class FakeResponse:
    """A fake aiohttp response object supporting .json() and .status."""

    def __init__(
        self,
        status: int = 200,
        json_data: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        """Initialize a fake response."""
        self.status = status
        self._json = json_data or {}
        self.headers = headers or {}

    async def json(self):
        """Return the JSON representation of the object."""
        return self._json


class FakeContextManager:
    """Async context manager wrapper for FakeResponse."""

    def __init__(self, response: FakeResponse) -> None:
        """Initialize a fake context manager."""
        self._response = response
        self._response = response

    async def __aenter__(self):
        """Enter the async context manager."""
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        """Exit the async context manager."""
        return False


# --------------------------------------------------------------------
# Helper to instantiate coordinator
# --------------------------------------------------------------------


def make_coordinator(hass: HomeAssistant, mock_config_entry):
    """Create a minimal coordinator for isolated unit testing."""
    return GitHubDataUpdateCoordinator(
        hass,
        mock_config_entry,
        client=AsyncMock(),
        repository="octocat/Hello-World",
        session=AsyncMock(),
        access_token="abc",
        update_interval=timedelta(seconds=1),  # MUST be timedelta
    )


# --------------------------------------------------------------------
# TESTS: _async_update_data exception handling
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_data_connection_exception(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test update data connection."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator._client.graphql.side_effect = GitHubConnectionException("boom")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_update_data_ratelimit_exception(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test update rate limit exception."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator._client.graphql.side_effect = GitHubRatelimitException("limit")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_update_data_generic_exception_logged(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test update data generic exception."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator._client.graphql.side_effect = GitHubException("unexpected")

    with patch("homeassistant.components.github.coordinator.LOGGER.exception") as log:
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        log.assert_called_once()


# --------------------------------------------------------------------
# TESTS: workflow run caching (ETag 304)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_runs_uses_cached_on_304(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test 304 exception."""
    coordinator = make_coordinator(hass, mock_config_entry)

    coordinator._workflow_runs = {"recent_runs": [{"id": 1}]}
    coordinator._workflow_etag = "W/etag-123"

    response = FakeResponse(status=304)

    def fake_get(*args, **kwargs):
        return FakeContextManager(response)

    coordinator._session.get = fake_get

    result = await coordinator._async_fetch_workflow_runs()
    assert result == {"recent_runs": [{"id": 1}]}


# --------------------------------------------------------------------
# TESTS: _workflow_headers()
# --------------------------------------------------------------------


def test_workflow_headers_includes_etag(hass: HomeAssistant, mock_config_entry) -> None:
    """Test if the header includes etag."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator._workflow_etag = "W/ABC"

    headers = coordinator._workflow_headers(include_etag=True)
    assert headers["If-None-Match"] == "W/ABC"


def test_workflow_headers_no_etag_when_disabled(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test header has no etag when disabled."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator._workflow_etag = "W/ABC"

    headers = coordinator._workflow_headers(include_etag=False)
    assert "If-None-Match" not in headers


# --------------------------------------------------------------------
# TESTS: _handle_event
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_event_triggers_refresh(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test event triggers refresh."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator.async_request_refresh = AsyncMock()

    event_type = next(iter(REFRESH_EVENT_TYPES))
    event = type("E", (), {"type": event_type})()

    await coordinator._handle_event(event)
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_event_ignores_unrelated_events(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test event handöe ignores unrelated events."""
    coordinator = make_coordinator(hass, mock_config_entry)
    coordinator.async_request_refresh = AsyncMock()

    event = type("E", (), {"type": "not_a_refresh_event"})()
    await coordinator._handle_event(event)

    coordinator.async_request_refresh.assert_not_called()


# --------------------------------------------------------------------
# TEST: _handle_error logs correctly
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_error_logs() -> None:
    """Test _handle_error logs correctly."""
    with patch("homeassistant.components.github.coordinator.LOGGER.error") as log:
        await GitHubDataUpdateCoordinator._handle_error(GitHubException("boom"))
        log.assert_called_once()


# --------------------------------------------------------------------
# TESTS: _async_workflow_runs fallback paths
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_workflow_runs_falls_back_to_cached(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test _async_workflow_runs fallback paths."""

    coordinator = make_coordinator(hass, mock_config_entry)

    coordinator._workflow_runs = {"recent_runs": [{"id": "cached"}]}

    def fake_get(*args, **kwargs):
        raise ClientError("down")

    coordinator._session.get = fake_get

    result = await coordinator._async_workflow_runs()
    assert result == {"recent_runs": [{"id": "cached"}]}


@pytest.mark.asyncio
async def test_async_workflow_runs_valueerror_fallback(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test _async_workflow_runs fallback valueerror."""
    coordinator = make_coordinator(hass, mock_config_entry)

    coordinator._workflow_runs = {"recent_runs": [{"id": 99}]}

    def fake_get(*args, **kwargs):
        raise ValueError("invalid json")

    coordinator._session.get = fake_get

    result = await coordinator._async_workflow_runs()
    assert result == {"recent_runs": [{"id": 99}]}


@pytest.mark.asyncio
async def test_trending_items_full_coverage(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Cover issue/discussion loops, type filtering, date filtering."""

    coordinator = make_coordinator(hass, mock_config_entry)

    now = datetime.now(UTC)
    recent = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=30)).isoformat()

    # Issues and discussions as the algorithm expects
    issues = [
        {"__typename": "PullRequest", "updatedAt": recent},  # skipped
        {"__typename": "Issue", "updatedAt": old},  # skipped (old)
        {
            "__typename": "Issue",
            "updatedAt": recent,
            "id": "i1",
            "title": "Issue 1",
            "url": "http://example/issue1",
            "comments": {"totalCount": 2},
            "reactions": {"totalCount": 3},
        },
    ]

    discussions = [
        {
            "updatedAt": recent,
            "id": "d1",
            "title": "Discussion 1",
            "url": "http://example/dis1",
            "comments": {"totalCount": 1},
            "reactions": {"totalCount": 1},
        },
    ]

    # Build minimal GraphQL-like payload for _build_trending_data
    graph_data = {
        "trending_issue_search": {"nodes": issues},
        "repository": {"trending_discussions": {"nodes": discussions}},
    }

    lookback_start = now - timedelta(days=7)

    trending = coordinator._build_trending_data(graph_data, lookback_start)

    # Verify highest activity item selected (issue has score 5 > discussion 2)
    assert trending["item_type"] == "issue"
    assert trending["title"] == "Issue 1"
    assert trending["url"] == "http://example/issue1"
    assert trending["activity_score"] == 5
    assert trending["lookback_days"] == coordinator._trending_lookback_days
