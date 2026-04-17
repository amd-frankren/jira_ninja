"""
Usage:
    python llm_openai_chat.py

"""

import openai
import os

ENV_LLM_GATEWAY_API_TOKEN = os.getenv("LLM_GATEWAY_API_TOKEN", "")
if not ENV_LLM_GATEWAY_API_TOKEN:
    raise RuntimeError("Missing required environment variable: LLM_GATEWAY_API_TOKEN")

ENV_LLM_GATEWAY_API_URL = os.getenv("LLM_GATEWAY_API_URL", "")
if not ENV_LLM_GATEWAY_API_URL:
    raise RuntimeError("Missing required environment variable: LLM_GATEWAY_API_URL")

client = openai.OpenAI(
    base_url=ENV_LLM_GATEWAY_API_URL,
    api_key="dummy",
    default_headers={
        "Ocp-Apim-Subscription-Key": ENV_LLM_GATEWAY_API_TOKEN,
        "user": os.getlogin()
    }
)

response = client.chat.completions.create(
    model="gpt-5-mini",
    max_tokens=200,
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the weather today?"}
    ]
)

print(response)
