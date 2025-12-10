# GitHub Integration – Enhanced Features Overview

This extended version of the Home Assistant GitHub integration adds three major capability groups:
1. **Issue Label Dashboard**
2. **Trending Issue/Discussion Sensor**
3. **GitHub Actions Workflow Activity Sensors**

These enhancements give you deep, real‑time visibility into repository health, activity, and automation performance.

---

## ✅ 1. Issue Label Dashboard

Track activity across specific labels in your repository without manually checking GitHub.

### **What it does**
For each label you configure, the integration creates:
- A sensor showing the **number of open issues** with that label.
- Attributes listing:
  - Issue numbers
  - URLs
  - Last time the data was fetched

This effectively gives you a dynamic, filterable per‑label issue dashboard inside Home Assistant.

### **How to enable**
1. Go to **Settings → Devices & Services → GitHub → Configure**  
2. Add one or more labels under **Issue Labels (comma‑separated)**  
3. Save and reload the integration

### **Sensors Created**
Example for label “bug”:

| Sensor | Description |
|--------|-------------|
| `sensor.<repo>_bug_issues` | Count of open issues with the “bug” label |

### **Attributes**
```yaml
issues:
  - number: 1234
    url: https://github.com/<repo>/issues/1234
last_checked: "2025‑01‑10T12:00:00Z"
label: "bug"
```

---

## 🔥 2. Trending Issue / Discussion (Feature 2)

Stay updated with the hottest topics in your repository without checking GitHub manually.

### **What it does**
Identifies the most active **Issue or Discussion** based on:
- Total **comments**
- Total **reactions**

within a configurable time window.

### **How to enable**
1. Go to **Settings → Devices & Services → GitHub**
2. Click **Configure**
3. Set **Trending Look‑back Days** (7 or 30)

### **Sensor Created**
| Sensor | Description |
|--------|-------------|
| `sensor.<repo>_trending_item` | Displays the title of the most active Issue or Discussion |

### **Attributes**
```yaml
activity_score: 68           # comments + reactions
item_type: "issue"           # or "discussion"
url: "https://github.com/<repo>/issues/12345"
creation_date: "2025‑12‑01T10:00:00+00:00"
lookback_days: 7
```

If nothing is active, the sensor reports:

```
No Trending Activity
```

---

## ⚙️ 3. GitHub Actions Workflow Sensors

Monitor GitHub Actions workflow activity directly from Home Assistant.

### **Data pulled**
The integration retrieves:
- Recent workflow runs (latest 5)
- Run status (success, failure, in progress, skipped, unknown)
- Branch names
- Start times
- Run URLs
- Success / failure counts
- ETag‑based caching for efficient polling

### **Sensors Created**
| Sensor | Purpose |
|--------|---------|
| `sensor.<repo>_workflow_runs` | Shows latest run title + status |
| `sensor.<repo>_workflow_summary` | Compact, human‑readable latest workflow entry |
| `sensor.<repo>_workflow_activity` | Pure status enum (`success`, `failure`, etc.) |

### **Attributes Example**
```yaml
successful_runs: 12
failed_runs: 3
in_progress_runs: 1
recent_runs:
  - run_id: 123456
    display_title: "CI – test suite"
    status: "success"
    run_started_at: "2025‑12‑10T09:00:00Z"
    html_url: "https://github.com/<repo>/actions/runs/123456"
latest_run_url: "https://github.com/<repo>/actions/runs/123456"
```

---

## 📊 Home Assistant Dashboard

A pre‑built dashboard can visualize:

- Workflow run summaries  
- Trending item  
- Recent workflow history  
- Issue label dashboards  
- History graphs  

Using standard Lovelace markdown + entity cards.


### Updated dashboard configuration example using Home Assistant built-in editor

```
views:
  - title: Github Dashboard
    path: github-dashboard
    icon: mdi:progress-clock
    cards:
      - type: entities
        title: Current run snapshot
        entities:
          - entity: sensor.home_assistant_core_workflow_runs
            name: Run title
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: head_branch
            name: Branch
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: status
            name: Status
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: conclusion
            name: Conclusion
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: run_started_at
            name: Started at
          - type: section
            label: Aggregates (latest page)
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: successful_runs
            name: Successful
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: failed_runs
            name: Failed
          - type: attribute
            entity: sensor.home_assistant_core_workflow_runs
            attribute: in_progress_runs
            name: In progress
      - type: grid
        columns: 2
        square: false
        cards:
          - type: markdown
            title: Current workflow status
            content: >-
              {% set s =
              state_attr('sensor.home_assistant_core_workflow_runs','status') %}
              {% set branch =
              state_attr('sensor.home_assistant_core_workflow_runs','head_branch')
              %} {% set started =
              state_attr('sensor.home_assistant_core_workflow_runs','run_started_at')
              %} {% set url =
              state_attr('sensor.home_assistant_core_workflow_runs','latest_run_url')
              %} {% set icon_map = {
                'success': '✅',
                'failure': '❌',
                'in_progress': '⏳',
                'skipped': '⏭️'
              } %} {% set icon = icon_map.get(s, '❔') %}

              **Status:** {{ icon }} {{ s | replace('_',' ') if s else 'unknown'
              }}

              **Branch:** `{{ branch if branch else 'unknown' }}`

              **Started:** {{ started[:16] if started else 'unknown' }}

              {% if url %} [Open current run on GitHub]({{ url }}) {% endif %}
          - type: markdown
            title: Trending issue/discussion
            content: >-
              {% set title = states('sensor.home_assistant_core_trending_item')
              %} {% set url =
              state_attr('sensor.home_assistant_core_trending_item','url') %} {%
              set score =
              state_attr('sensor.home_assistant_core_trending_item','activity_score')
              %} {% set kind =
              state_attr('sensor.home_assistant_core_trending_item','item_type')
              %} {% set lookback =
              state_attr('sensor.home_assistant_core_trending_item','lookback_days')
              %}

              {% if title == 'No Trending Activity' %} 🔇 No trending activity
              in the last {{ lookback }} days. {% else %} 🔥 **{{ title }}**

              - Type: `{{ kind or 'unknown' }}` - Activity score: `{{ score if
              score is not none else 0 }}` - Lookback window: `{{ lookback }}
              day(s)`

              {% if url %} [Open on GitHub]({{ url }}) {% endif %} {% endif %}
      - type: grid
        columns: 2
        square: false
        cards:
          - type: markdown
            title: Recent runs (top 5)
            content: >-
              {% set runs =
              state_attr('sensor.home_assistant_core_workflow_runs','recent_runs')
              or [] %} {% set icon_map = {
                'success': '✅',
                'failure': '❌',
                'in_progress': '⏳',
                'skipped': '⏭️'
              } %} {% if runs %} {% for run in runs[:5] %} {% set icon =
              icon_map.get(run.status, '❔') %} - **{{ icon }} {{
              run.display_title }}**  
                Status: `{{ run.status | replace('_',' ') }}`  
                Branch: `{{ run.head_branch }}`  
                Started: {{ run.run_started_at[:16] if run.run_started_at else 'unknown' }}  
                [Open on GitHub]({{ run.html_url }})

              {% endfor %} {% else %} No recent runs. {% endif %}
          - type: markdown
            title: GitHub label issues
            content: >-
              {% set labels = [
                {'name': 'bug', 'entity': 'sensor.home_assistant_core_bug_issues'},
                {'name': 'good first issue', 'entity': 'sensor.home_assistant_core_good_first_issue_issues'},
                {'name': 'documentation', 'entity': 'sensor.home_assistant_core_documentation_issues'},
                {'name': 'integration', 'entity': 'sensor.home_assistant_core_integration_issues'},
              ] %}

              {% for label in labels %} {% set ent = label.entity %} {% set
              count = states(ent) %} {% set issues = state_attr(ent, 'issues')
              or [] %} {% set last = state_attr(ent, 'last_checked') %}

              ### {{ label.name | title }}

              - Open issues: {{ count if count not in ['unavailable','unknown']
              else 'unknown' }} - Last checked: {{ last or 'pending' }}

              {% if issues %} **Issue links:** {% for issue in issues %} - [#{{
              issue.number }}]({{ issue.url }}) {% endfor %} {% else %} No open
              issues for this label. {% endif %}

              --- {% endfor %}
      - type: history-graph
        entities:
          - sensor.home_assistant_core_workflow_runs
        hours_to_show: 24
        refresh_interval: 0
```

### ⚠️ Dashboard Reusability Note

The example dashboard configuration uses entity IDs that include the repository name (e.g., `sensor.home_assistant_core_workflow_runs`).
Because Home Assistant generates entity IDs based on the repository selected during setup, dashboard configurations are not automatically reusable if:

- you track a different repository,
- you rename the repository, or
- you use multiple repositories in parallel.

If you change the repository, you must update the entity IDs in the dashboard YAML manually to match the new integration entities.

This limitation comes from Home Assistant’s naming conventions and is expected behavior.

---

## ⚡ Summary of Enhancements

| Feature | Purpose |
|--------|---------|
| Issue Label Dashboard | Track per‑label issue counts with details |
| Trending Issue/Discussion | Surface the most active GitHub item |
| Workflow Activity Sensors | Bring GitHub Actions visibility into HA |
| Dashboard Layout | Quick visual insights for developers/Admins |

---

## 📝 Requirements

- Home Assistant 2024.12+
- Integration version including these enhancement files
- GitHub Personal Access Token or OAuth device login

---

## 📁 Where Each Feature Lives in the Codebase

| Feature | Key Files |
|---------|-----------|
| Issue Dashboard | `sensor.py`, `coordinator.py` |
| Trending Item | `sensor.py`, `coordinator.py`, `README_Feature2.md` |
| Workflow Sensors | `sensor.py`, `coordinator.py` |
| Options UI | `config_flow.py` |
| Translations | `translations/en.json` |

---

## 📦 Final Notes

These enhancements aim to make the GitHub integration far more useful for automation workflows, repository monitoring, and developer productivity – delivering real insights from GitHub directly into Home Assistant.