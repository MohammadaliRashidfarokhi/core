"""Custom data update coordinator for the GitHub integration."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any

from aiogithubapi import (
    GitHubAPI,
    GitHubConnectionException,
    GitHubEventModel,
    GitHubException,
    GitHubRatelimitException,
    GitHubResponseModel,
)
from aiohttp import ClientError, ClientSession

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import LOGGER, REFRESH_EVENT_TYPES

WORKFLOW_PAGE_SIZE = 25
WORKFLOW_MINIMUM_RUNS = 5
WORKFLOW_RECENT_RUNS = 5

GRAPHQL_REPOSITORY_QUERY = """
query ($owner: String!, $repository: String!) {
  rateLimit {
    cost
    remaining
  }
  repository(owner: $owner, name: $repository) {
    default_branch_ref: defaultBranchRef {
      commit: target {
        ... on Commit {
          message: messageHeadline
          url
          sha: oid
        }
      }
    }
    stargazers_count: stargazerCount
    forks_count: forkCount
    full_name: nameWithOwner
    id: databaseId
    watchers(first: 1) {
      total: totalCount
    }
    discussion: discussions(
      first: 1
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      total: totalCount
      discussions: nodes {
        title
        url
        number
      }
    }
    issue: issues(
      first: 1
      states: OPEN
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      total: totalCount
      issues: nodes {
        title
        url
        number
      }
    }
    pull_request: pullRequests(
      first: 1
      states: OPEN
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      total: totalCount
      pull_requests: nodes {
        title
        url
        number
      }
    }
    release: latestRelease {
      name
      url
      tag: tagName
    }
    refs(
      first: 1
      refPrefix: "refs/tags/"
      orderBy: {field: TAG_COMMIT_DATE, direction: DESC}
    ) {
      tags: nodes {
        name
        target {
          url: commitUrl
        }
      }
    }
  }
}
"""

type GithubConfigEntry = ConfigEntry[dict[str, GitHubDataUpdateCoordinator]]


class GitHubDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data update coordinator for the GitHub integration."""

    config_entry: GithubConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: GithubConfigEntry,
        client: GitHubAPI,
        repository: str,
        session: ClientSession,
        access_token: str,
        update_interval: timedelta,
    ) -> None:
        """Initialize GitHub data update coordinator base class."""
        self.repository = repository
        self._client = client
        self._session = session
        self._access_token = access_token
        self._last_response: GitHubResponseModel[dict[str, Any]] | None = None
        self._subscription_id: str | None = None
        self.data = {}
        self._workflow_etag: str | None = None
        self._workflow_runs: dict[str, Any] = {"recent_runs": []}

        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=repository,
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> GitHubResponseModel[dict[str, Any]]:
        """Update data."""
        owner, repository = self.repository.split("/")
        try:
            response = await self._client.graphql(
                query=GRAPHQL_REPOSITORY_QUERY,
                variables={"owner": owner, "repository": repository},
            )
        except (GitHubConnectionException, GitHubRatelimitException) as exception:
            # These are expected and we dont log anything extra
            raise UpdateFailed(exception) from exception
        except GitHubException as exception:
            # These are unexpected and we log the trace to help with troubleshooting
            LOGGER.exception(exception)
            raise UpdateFailed(exception) from exception

        self._last_response = response
        repository_data = response.data["data"]["repository"]
        repository_data["workflow_runs"] = await self._async_workflow_runs()
        return repository_data

    async def _async_workflow_runs(self) -> dict[str, Any]:
        """Retrieve workflow run information for the repository."""
        try:
            return await self._async_fetch_workflow_runs()
        except (TimeoutError, ClientError, ValueError) as err:
            LOGGER.debug(
                "Unable to refresh workflow runs for %s: %s", self.repository, err
            )
        return self._workflow_runs

    async def _async_fetch_workflow_runs(self) -> dict[str, Any]:
        """Fetch workflow runs using the GitHub REST API."""
        runs: list[dict[str, Any]] = []
        page = 1
        include_etag = True
        while len(runs) < WORKFLOW_MINIMUM_RUNS:
            headers = self._workflow_headers(include_etag=include_etag)
            params = {"per_page": WORKFLOW_PAGE_SIZE, "page": page}
            async with self._session.get(
                f"https://api.github.com/repos/{self.repository}/actions/runs",
                headers=headers,
                params=params,
            ) as response:
                if include_etag and response.status == HTTPStatus.NOT_MODIFIED:
                    return self._workflow_runs
                response.raise_for_status()
                payload = await response.json()
                response_headers = response.headers

            if include_etag:
                self._workflow_etag = response_headers.get(
                    "ETag"
                ) or response_headers.get("Etag")
                include_etag = False
            runs_page = payload.get("workflow_runs", [])
            runs.extend(runs_page)
            if len(runs_page) < WORKFLOW_PAGE_SIZE:
                break
            page += 1

        self._workflow_runs = {"recent_runs": runs[:WORKFLOW_RECENT_RUNS]}
        return self._workflow_runs

    def _workflow_headers(self, *, include_etag: bool) -> dict[str, str]:
        """Return headers for workflow run requests."""
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": SERVER_SOFTWARE,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if include_etag and self._workflow_etag:
            headers["If-None-Match"] = self._workflow_etag
        return headers

    async def _handle_event(self, event: GitHubEventModel) -> None:
        """Handle an event."""
        if event.type in REFRESH_EVENT_TYPES:
            await self.async_request_refresh()

    @staticmethod
    async def _handle_error(error: GitHubException) -> None:
        """Handle an error."""
        LOGGER.error("An error occurred while processing new events - %s", error)

    async def subscribe(self) -> None:
        """Subscribe to repository events."""
        self._subscription_id = await self._client.repos.events.subscribe(
            self.repository,
            event_callback=self._handle_event,
            error_callback=self._handle_error,
        )
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.unsubscribe)

    def unsubscribe(self, *args: Any) -> None:
        """Unsubscribe to repository events."""
        self._client.repos.events.unsubscribe(subscription_id=self._subscription_id)
