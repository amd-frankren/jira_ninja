# Jira Ticket QA WebUI

```powershell
$env:LLM_GATEWAY_API_URL = "https://llm-api.amd.com/OpenAI"
$env:LLM_GATEWAY_API_TOKEN = "your_token_here"
python -m uvicorn server:app --host 127.0.0.1 --port 8090
```

This is an independent project directory (without modifying your existing code), providing:

- Web Server (FastAPI)
- Web UI (plain HTML/CSS/JS)
- Jira Ticket Q&A based on LLM + MCP tools
- Real-time MCP call process and results display (SSE streaming events)
- Interactive flow for follow-up questions and user choices

---

## Directory Structure

```text
jira_ticket_webapp/
  ├─ server.py
  ├─ requirements.txt
  ├─ README.md
  └─ static/
     └─ index.html
```

---

## Features

- Frontend endpoint: `POST /api/chat/stream`
- Return type: `text/event-stream` (SSE)
- Main events:
  - `status`: status info
  - `tool_start`: MCP tool call started
  - `tool_result`: MCP tool result
  - `tool_error`: MCP tool error
  - `ask_user`: model follow-up question with optional choices
  - `final`: final answer

---

## Prerequisites

Make sure these MCP servers are accessible first:

- `http://127.0.0.1:8000/mcp` (jira_external)
- `http://127.0.0.1:8002/mcp` (jira_internal)

You also need LLM gateway environment variables:

- `LLM_GATEWAY_API_URL`
- `LLM_GATEWAY_API_TOKEN`

---

## Install and Run

Run in the repository root (Windows cmd / PowerShell both work):

```bash
pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8090 --reload
```

Open in browser:

```text
http://127.0.0.1:8090
```

---

## Optional Environment Variables

### LLM
- `WEBUI_LLM_MODEL` (default: `gpt-5-mini`)
- `WEBUI_LLM_MAX_TOKENS` (default: `1200`)

### MCP URL (override defaults)
- `SCET_MCP_URL`
- `JIRA_INTERNAL_MCP_URL`

### MCP auth token (if your MCP server requires it)
- `SCET_MCP_AUTH_TOKEN`
- `JIRA_INTERNAL_MCP_AUTH_TOKEN`

---

## About Follow-up Questions / User Choices

If the backend model outputs the following JSON (strict JSON), frontend interaction is triggered:

```json
{
  "type": "ask_user",
  "question": "Which project do you want to query?",
  "options": ["SCET", "ACV2", "PLAT"]
}
```

The frontend renders buttons. After user selection, it continues to the next Q&A round while preserving the same session context.
