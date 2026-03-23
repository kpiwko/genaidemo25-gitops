"""
title: Guardrails Block Message Filter
author: kpiwko
author_url: https://github.com/kpiwko/genaidemo25-gitops
funding_url: https://github.com/open-webui
version: 0.1

Intercepts responses blocked by the TrustyAI guardrails gateway and replaces
the empty response with a user-friendly bilingual (English + Czech) message.

The TrustyAI built-in regex detector blocks prompts containing swear words in
Czech and English. When blocked, the gateway returns choices: [] with a
warnings field of type UNSUITABLE_INPUT. This filter turns that into a visible
message in the chat UI.

To install: Admin Panel → Functions → + → paste this file → enable globally.
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
                "⚠️ Your message was blocked because it contained inappropriate language. "
                "Please rephrase your question without swear words."
            ),
            description="Message shown to the user when their prompt is blocked (English).",
        )
        message_cs: str = Field(
            default=(
                "⚠️ Váš dotaz byl zablokován, protože obsahoval nevhodný výraz. "
                "Zkuste ho přeformulovat bez vulgarit."
            ),
            description="Message shown to the user when their prompt is blocked (Czech).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        choices = body.get("choices", None)
        warnings = body.get("warnings") or []

        blocked = (
            isinstance(choices, list)
            and len(choices) == 0
            and any(w.get("type") == "UNSUITABLE_INPUT" for w in warnings)
        )

        if blocked:
            body["choices"] = [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"{self.valves.message_en}\n\n{self.valves.message_cs}",
                    },
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ]

        return body
