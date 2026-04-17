# 01 Overview

This is a backend service that can be used with a frontend Copilot agent, or can automatically detect, analyze, and add comments to tickets on the backend.


# 02 User guide

Step 1: Export envs

    export EXTERNAL_JIRA_TOKEN=...[Your External Jira Token]...

    export INTERNAL_JIRA_TOKEN=...[Your Internal Jira Token]...

    export LLM_GATEWAY_API_TOKEN=...[Your LLM Gateway API Token]...

    export LLM_GATEWAY_API_URL=...[The LLM Gateway API URL]...

    export MCP_SERVER_URL_ATLASSIAN_INTERNAL=...[Internal Atlassian MCP server URL]...

    export SHAREPOINT_HOST_NAME=...[SharePoint Host Name]...

    export PLAT_PROJECT_ISSUES_URL=...[The PLAT Project URL]...


Step 2: Launch the scrip

    # launch without backdate
    python python main.py

    
    # lanuch woith 60 mins backdate
    python python main.py --since-minutes 60


