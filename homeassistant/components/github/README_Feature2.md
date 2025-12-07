###  New Feature: Trending Issue

Stay updated with the hottest topics in your repository without checking GitHub manually.

**What it does:** Identifies the most active Issue or Discussion based on total comments and reactions within a configurable time window.

**How to enable:**
1. Go to **Settings > Devices & Services > GitHub**
2. Click **Configure** on your repository integration
3. Set **"Trending Look-back Days"** (Options: 7 or 30 days, Default: 7)
   - *Note: This filters items by their last update date to focus on recent activity.*

**Sensor Created:**
- `sensor.<repo_name>_trending_item`: Displays the title of the trending item as its state.

**Attributes:**
- `activity_score`: Total count of (comments + reactions)
- `item_type`: Type of item - "issue" or "discussion"
- `url`: Direct link to view the item on GitHub
- `creation_date`: When the item was created
- `lookback_days`: Current time window setting

**Example State:**
```yaml
sensor.home_assistant_core_trending_item:
  state: "Fix critical memory leak"
  activity_score: 68
  item_type: "issue"
  url: "[https://github.com/home-assistant/core/issues/12345](https://github.com/home-assistant/core/issues/12345)"
  creation_date: "2025-12-01T10:00:00+00:00"
