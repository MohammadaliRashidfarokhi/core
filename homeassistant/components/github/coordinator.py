"""Custom data update coordinator for the GitHub integration."""

from __future__ import annotations

from datetime import datetime, timedelta
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
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ISSUE_LABELS,
    CONF_TRENDING_LOOKBACK_DAYS,
    DEFAULT_ISSUE_LABELS,
    DEFAULT_TRENDING_LOOKBACK_DAYS,
    LOGGER,
    REFRESH_EVENT_TYPES,
    TRENDING_REFRESH_INTERVAL,
)

WORKFLOW_PAGE_SIZE = 25
WORKFLOW_MINIMUM_RUNS = 5
WORKFLOW_RECENT_RUNS = 5

GRAPHQL_REPOSITORY_QUERY = """
query ($owner: String!, $repository: String!, $issueQuery: String!) {
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
    trending_discussions: discussions(
      first: 10
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        title
        url
        createdAt
        updatedAt
        comments {
          totalCount
        }
        reactions {
          totalCount
        }
      }
    }
  }
  trending_issue_search: search(
    query: $issueQuery
    type: ISSUE
    first: 10
  ) {
    nodes {
      ... on Issue {
        title
        url
        createdAt
        updatedAt
        comments {
          totalCount
        }
        reactions {
          totalCount
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
        self._trending_data: dict[str, Any] = {}
        self._last_trending_fetch: datetime | None = None
        self._trending_lookback_days = config_entry.options.get(
            CONF_TRENDING_LOOKBACK_DAYS, DEFAULT_TRENDING_LOOKBACK_DAYS
        )
        self._issue_labels: list[str] = config_entry.options.get(
            CONF_ISSUE_LABELS, DEFAULT_ISSUE_LABELS
        )

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
        lookback_start = dt_util.utcnow() - timedelta(days=self._trending_lookback_days)
        issue_query = (
            f"repo:{self.repository} is:issue updated:>={lookback_start.date()} "
            "sort:updated-desc"
        )
        try:
            response = await self._client.graphql(
                query=GRAPHQL_REPOSITORY_QUERY,
                variables={
                    "owner": owner,
                    "repository": repository,
                    "issueQuery": issue_query,
                },
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
        repository_data["trending"] = self._build_trending_data(
            response.data["data"],
            lookback_start,
        )
        repository_data["label_issues"] = await self._async_label_issues()
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

    def _build_trending_data(
        self,
        graph_data: dict[str, Any],
        lookback_start: datetime,
    ) -> dict[str, Any]:
        """Return trending issue/discussion data with cached refresh."""
        now = dt_util.utcnow()
        if (
            self._last_trending_fetch
            and now - self._last_trending_fetch < TRENDING_REFRESH_INTERVAL
        ):
            return self._trending_data

        issue_search = graph_data.get("trending_issue_search") or {}
        repository_data = graph_data.get("repository") or {}
        discussion_nodes = (
            repository_data.get("trending_discussions", {}).get("nodes") or []
        )
        issue_nodes = issue_search.get("nodes") or []

        def _parse_datetime(value: str | None) -> datetime | None:
            if not value:
                return None
            return dt_util.parse_datetime(value)

        def _score_item(item: dict[str, Any], *, item_type: str) -> dict[str, Any]:
            comments = item.get("comments", {}).get("totalCount") or 0
            reactions = item.get("reactions", {}).get("totalCount") or 0
            updated_at = _parse_datetime(item.get("updatedAt"))
            created_at = item.get("createdAt")
            return {
                "title": item.get("title"),
                "url": item.get("url"),
                "item_type": item_type,
                "activity_score": comments + reactions,
                "created_at": created_at,
                "updated_at": updated_at,
            }

        items: list[dict[str, Any]] = []
        for issue in issue_nodes:
            if issue.get("__typename") and issue.get("__typename") != "Issue":
                continue
            candidate = _score_item(issue, item_type="issue")
            if candidate["updated_at"] and candidate["updated_at"] < lookback_start:
                continue
            items.append(candidate)

        for discussion in discussion_nodes:
            candidate = _score_item(discussion, item_type="discussion")
            if candidate["updated_at"] and candidate["updated_at"] < lookback_start:
                continue
            items.append(candidate)

        best_item: dict[str, Any] | None = None
        if items:
            best_item = max(
                items,
                key=lambda item: (
                    item["activity_score"],
                    item["updated_at"] or datetime.min.replace(tzinfo=dt_util.UTC),
                ),
            )

        trending = {
            "title": best_item["title"] if best_item else None,
            "url": best_item["url"] if best_item else None,
            "item_type": best_item["item_type"] if best_item else None,
            "activity_score": best_item["activity_score"] if best_item else 0,
            "creation_date": best_item["created_at"] if best_item else None,
            "lookback_days": self._trending_lookback_days,
        }

        self._trending_data = trending
        self._last_trending_fetch = now
        return trending

    async def _async_label_issues(self) -> dict[str, Any]:
        """Fetch open issue counts for configured labels."""
        if not self._issue_labels:
            return {}

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": SERVER_SOFTWARE,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        results: dict[str, Any] = {}
        for label in self._issue_labels:
            query = f'repo:{self.repository} state:open type:issue label:"{label}"'
            params: list[tuple[str, str]] = [("q", query), ("per_page", "50")]
            async with self._session.get(
                "https://api.github.com/search/issues", headers=headers, params=params
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            items = payload.get("items", [])
            issues = [
                {
                    "number": item.get("number"),
                    "url": item.get("html_url"),
                }
                for item in items
            ]
            results[label.lower()] = {
                "count": payload.get("total_count", 0),
                "issues": issues,
                "last_checked": dt_util.utcnow().isoformat(),
            }
        return results

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
