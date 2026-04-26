# 1 Overview


# 2 SCET Monitor

Step 1: Setup Envs

`$env:EXTERNAL_JIRA_URL = "https://ontrack.amd.com"`

`$env:EXTERNAL_JIRA_TOKEN = "$token"`

Step 2: Launch it

`python main.py --interval 60 --since-minutes 60`


# 3 Web Workspace

Step 1: Setup Envs

`$env:LLM_GATEWAY_API_URL = "https://llm-api.amd.com/OpenAI"`
`$env:LLM_GATEWAY_API_TOKEN = "$token"`

Step 2: Launch the Web server

`python -m uvicorn server:app --host 127.0.0.1 --port 8090`

Step 3: Use it in your Web browser

`http://127.0.0.1:8090/`


