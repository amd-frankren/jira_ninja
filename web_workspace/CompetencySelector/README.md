# Competency Selector

An internal AMD tool that classifies SCET ticket root causes into structured **Q1** (first-level component category) and **Q2** (second-layer sub-category) competency codes. Supports AI auto-classification from pasted text or JIRA ticket URLs, manual selection, and a built-in feedback loop.

---

## Quick Start (for Cline / AI assistant)

**Project folder:**
```
C:\Users\yuan-lin\Desktop\Icons\AI - Vibe coding\
Competency\CompetencySelector - Shale deployment - 04162026\
```

**Key files:**
| File | Purpose |
|---|---|
| `server.py` | Python Flask backend (AI proxy + JIRA/SharePoint fetch + feedback) |
| `index.html` | React frontend (single-page app, 3-page wizard) |
| `requirements.txt` | Python dependencies |
| `.env` | Runtime config – contains secrets, **edit here to change LLM model** |
| `.env.example` | Config template |
| `start.bat` | Windows manual startup script |
| `_service_runner.bat` | Called by Task Scheduler for auto-start (do NOT rename) |
| `install-service.bat` | One-time service installer (run as Administrator) |
| `uninstall-service.bat` | Removes scheduled tasks |

---

## Deployment Info

| Item | Value |
|---|---|
| **Server IP** | 10.95.37.121 |
| **Port** | 5000 |
| **User URL** | http://10.95.37.121:5000 |
| **Run mode** | Windows Task Scheduler (auto-start at boot) |
| **Python path** | `C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe` |

---

## Architecture

```
Browser (index.html – React)
  │
  ├── POST /api/ai            → AI classification (proxied to AMD LLM)
  ├── POST /api/fetch-ticket  → Fetch JIRA ticket / SharePoint JSON
  ├── POST /api/feedback      → Record user correction → feedback_logs.json
  └── GET  /api/health        → Config status check

Fetch Ticket Priority:
  1. JIRA REST API  (if JIRA_TOKEN set in .env)   ← recommended
  2. SharePoint Graph API  (az CLI token, AZURE_CONFIG_DIR set)
```

---

## Features

### AI Auto-Classify
**Tab 1 – Paste Root Cause**
Paste the root cause description → AI classifies → Shows Q1 + Q2 result

**Tab 2 – JIRA Ticket URL**
Enter `https://ontrack.amd.com/browse/SCET-XXXX` → Server fetches ticket content → AI classifies

### Classification Logic (AI Prompt Design)
- **Comments have highest weight** (newest comment first, up to 500 chars)
- **Two-step reasoning**: Step 1 = Q1 category, Step 2 = Q2 sub-category within that Q1
- **Few-shot learning**: Past user corrections injected automatically into prompt

### Result Page
- Shows Q1 + Q2 with copy button
- AI reasoning explanation
- Feedback / correction submission

### Manual Selection
3-page wizard: Q1 → Q2 → Result (with Edit Q2 back button)

---

## Windows Service (Auto-Start)

The service is managed by two Windows Scheduled Tasks:

| Task Name | Trigger | Action |
|---|---|---|
| `CompetencySelector-Server` | System boot (30s delay) | Runs `_service_runner.bat` |
| `CompetencySelector-TokenRefresh` | Daily 06:00 AM | Refreshes Azure CLI token |

### Service management (Admin CMD on Server)
```bat
# Start immediately
schtasks /run /tn "CompetencySelector-Server"

# Stop
schtasks /end /tn "CompetencySelector-Server"

# Restart after .env change
schtasks /end /tn "CompetencySelector-Server"
schtasks /run /tn "CompetencySelector-Server"

# Check status
schtasks /query /tn "CompetencySelector-Server"
```

### Logs
```bat
type "C:\CompetencySelector\04162026\server.log"
```

---

## Environment Variables (.env)

```env
PORT=5000
HOST=0.0.0.0
DEPLOY_HOST=10.95.37.121

# AMD AI endpoint
AI_ENDPOINT=https://llm-api.amd.com/OnPrem/chat/completions
SUB_KEY=your_key_here       # ⚠️ REQUIRED
API_USER=competency-selector-index-1
API_VERSION=preview
LLM_MODEL=GPT-oss-120B      # ← Change model here
LLM_MAX_TOKENS=1024
AI_TIMEOUT=90

# JIRA API (recommended for JIRA Ticket URL feature)
JIRA_BASE_URL=https://ontrack.amd.com
JIRA_TOKEN=                 # Personal Access Token from ontrack.amd.com → Profile → PAT

# SharePoint (fallback if JIRA_TOKEN not set)
SHAREPOINT_HOST_NAME=amdcloud.sharepoint.com
SHAREPOINT_SITE_PATH=/sites/SCPI
SHAREPOINT_DRIVE_NAME=Documents
SHAREPOINT_REMOTE_FOLDER=SCETS/SCET_export_minified
SHAREPOINT_ACCESS_TOKEN=    # Leave blank to use az CLI

# Feedback
FEEDBACK_LOG=feedback_logs.json
DEBUG=false
```

> **After editing .env**: Must restart service (`schtasks /end` + `schtasks /run`)
> **index.html changes**: No restart needed, just F5 in browser

---

## JIRA Ticket URL Feature

The system fetches SCET ticket content from JIRA (or SharePoint as fallback) and sends it to AI for classification.

**Fields extracted (newest comments = highest weight):**
1. `[LATEST COMMENT]` – 500 chars (most important)
2. `[COMMENT-2]` to `[COMMENT-6]` – 400/300/200/150/100 chars
3. `[Root Cause]` – formal root cause field
4. `[Root Cause (Analysis)]`
5. `[Description]` – original description (background context only)
6. `[Workaround]`, `[Problem Symptom]`, `[Steps]`

---

## Q1/Q2 Category Reference

| Q1 ID | Category |
|---|---|
| a | 3rd Party |
| b | Firmware |
| c | CPU |
| d | Platform Hardware |
| e | Tool |
| f | SI |
| g | OS |
| h | Electrical Validation |
| i | Design Collateral |
| j | Design Review |
| k | Others |

---

## Maintenance

### Token expiry (Azure CLI)
If JIRA Ticket URL fails with "Cannot obtain SharePoint/Graph access token":
```bat
az login --use-device-code --allow-no-subscriptions
```
(Run on Server, authenticate from your own browser)

### JIRA Token expiry (if JIRA_TOKEN is set)
Regenerate at: `https://ontrack.amd.com` → Profile → Personal Access Tokens

### Health check
```
http://10.95.37.121:5000/api/health
```

---

## Known Issues / Future Plans

- [ ] Display existing Competency field from JIRA ticket as reference
- [ ] Batch processing multiple SCET tickets at once
- [ ] Dashboard showing classification history / accuracy rate
- [ ] Integrate with JIRA to auto-update the Competency field

---

## Internal Use Only

> **AMD Chief of Staff Office** — For internal use only. Do not distribute externally.
