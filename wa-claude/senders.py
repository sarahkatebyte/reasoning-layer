"""
senders.py - handles actually talking to the target endpoint.

Two senders, same contract:
    send(prompt: str) -> str

That's it. runner.py doesn't care which one it's using.
"""

import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()


def expand_env_vars(value: str) -> str:
    """
    Replaces ${VAR_NAME} in strings with the actual env var value.
    Raises clearly if a var is missing - better than silently sending a broken header.
    """
    def replacer(match):
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            raise EnvironmentError(
                f"Missing required env var: {var_name}\n"
                f"Add it to your .env file or export it in your shell."
            )
        return val

    return re.sub(r'\$\{([^}]+)\}', replacer, value)


def expand_headers(headers: dict) -> dict:
    """Expand env vars in all header values."""
    return {k: expand_env_vars(v) for k, v in headers.items()}


class OpenAISender:
    """
    Sends prompts to any OpenAI-compatible endpoint.
    That includes OpenAI, Anthropic (with openai compat layer),
    local Ollama, vLLM, etc.

    The request shape is always:
        POST /v1/chat/completions
        { "model": "...", "messages": [{"role": "user", "content": prompt}] }
    """

    def __init__(self, url: str, headers: dict, model: str = "gpt-4o"):
        self.url = url
        self.headers = expand_headers(headers)
        self.model = model

    def send(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = requests.post(self.url, json=payload, headers=self.headers)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


class HTTPSender:
    """
    Sends prompts to an arbitrary HTTP endpoint.
    The user tells us which field the prompt goes in via config.

    Example config:
        mode: http
        field: message   <- prompt goes in body["message"]

    This covers custom APIs, internal tools, wrapped models, whatever.
    """

    def __init__(self, url: str, headers: dict, field: str = "message"):
        self.url = url
        self.headers = expand_headers(headers)
        self.field = field

    def send(self, prompt: str) -> str:
        payload = {self.field: prompt}
        response = requests.post(self.url, json=payload, headers=self.headers)
        response.raise_for_status()
        data = response.json()

        # Try common response field names - different APIs use different shapes
        for key in ("response", "message", "content", "output", "text"):
            if key in data:
                return data[key]

        # Last resort - return the whole response as a string
        return str(data)
