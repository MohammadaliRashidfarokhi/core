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

These enhancements aim to make the GitHub integration far more useful for automation workflows, repository monitoring, and developer productivity—delivering real insights from GitHub directly into Home Assistant.
