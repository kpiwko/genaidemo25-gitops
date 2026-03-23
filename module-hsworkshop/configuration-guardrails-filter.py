"""
title: Guardrails Block Message Filter
author: kpiwko
author_url: https://github.com/kpiwko/genaidemo25-gitops
funding_url: https://github.com/open-webui
version: 0.3

NOTE: As of v0.3 the friendly blocked message is injected by the
guardrails-proxy (profanity-detector.yaml) before the response reaches
OpenWebUI. This filter is kept as a fallback only and does not need to
be enabled unless the proxy is not deployed.

The proxy converts the TrustyAI UNSUITABLE_INPUT empty-choices response
into a proper assistant message, allowing the conversation to continue
normally after a blocked prompt.

To install: Admin Panel → Functions → + → paste this file.
"""

from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0, description="Priority level for the filter operations."
        )
        message_en: str = Field(
            default=(
                "\u26a0\ufe0f Your message was blocked because it contained inappropriate language. "
                "Please rephrase your question without swear words."
            ),
            description="Message shown when a prompt is blocked (English).",
        )
        message_cs: str = Field(
            default=(
                "\u26a0\ufe0f V\u00e1\u0161 dotaz byl zablokov\u00e1n, proto\u017ee obsahoval nevhodn\u00fd v\u00fdraz. "
                "Zkuste ho p\u0159eformulovat bez vulgarit."
            ),
            description="Message shown when a prompt is blocked (Czech).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        choices  = body.get("choices", None)
        warnings = body.get("warnings") or []

        blocked = (
            isinstance(choices, list)
            and len(choices) == 0
            and any(w.get("type") == "UNSUITABLE_INPUT" for w in warnings)
        )

        if blocked:
            body["choices"] = [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"{self.valves.message_en}\n\n{self.valves.message_cs}",
                },
                "finish_reason": "stop",
                "logprobs": None,
            }]

        return body
