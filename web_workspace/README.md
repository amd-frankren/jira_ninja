# Jira Ticket QA WebUI


$env:LLM_GATEWAY_API_URL = "https://llm-api.amd.com/OpenAI"
env:LLM_GATEWAY_API_TOKEN = "eb9f26fa88044526818ca5a49b52124c"
python -m uvicorn server:app --host 127.0.0.1 --port 8090

一个独立的新项目目录（未修改你现有代码），提供：

- Web Server（FastAPI）
- Web UI（原生 HTML/CSS/JS）
- 基于 LLM + MCP tools 的 Jira Ticket 问答
- 实时显示 MCP 调用过程与结果（SSE 流式事件）
- 支持“反问用户 / 让用户选择”的交互流程

---

## 目录结构

```text
jira_ticket_webapp/
  ├─ server.py
  ├─ requirements.txt
  ├─ README.md
  └─ static/
     └─ index.html
```

---

## 功能说明

- 前端调用：`POST /api/chat/stream`
- 返回类型：`text/event-stream`（SSE）
- 主要事件：
  - `status`：状态信息
  - `tool_start`：开始调用 MCP 工具
  - `tool_result`：工具返回结果
  - `tool_error`：工具调用错误
  - `ask_user`：模型反问用户，并可带选项
  - `final`：最终回答

---

## 运行前准备

你需要先确保这几个 MCP server 已可访问：

- `http://127.0.0.1:8000/mcp`（scet）
- `http://127.0.0.1:8002/mcp`（jira_internal）

同时需要 LLM 网关环境变量：

- `LLM_GATEWAY_API_URL`
- `LLM_GATEWAY_API_TOKEN`

---

## 安装与启动

在仓库根目录执行（Windows cmd / PowerShell 都可以）：

```bash
pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8090 --reload
```

浏览器打开：

```text
http://127.0.0.1:8090
```

---

## 可选环境变量

### LLM
- `WEBUI_LLM_MODEL`（默认 `gpt-5-mini`）
- `WEBUI_LLM_MAX_TOKENS`（默认 `1200`）

### MCP URL（可覆盖默认值）
- `SCET_MCP_URL`
- `JIRA_INTERNAL_MCP_URL`

### MCP 鉴权 Token（如你的 MCP server 需要）
- `SCET_MCP_AUTH_TOKEN`
- `JIRA_INTERNAL_MCP_AUTH_TOKEN`

---

## 关于“反问用户 / 选择项”

后端约定模型输出如下 JSON（严格 JSON）即触发前端交互：

```json
{
  "type": "ask_user",
  "question": "你要查询哪个项目？",
  "options": ["SCET", "ACV2", "PLAT"]
}
```

前端会渲染按钮；用户点击后会继续下一轮问答并保留同一个会话上下文。
