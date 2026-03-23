"""
title: Guardrails Block Message Filter
author: kpiwko
author_url: https://github.com/kpiwko/genaidemo25-gitops
funding_url: https://github.com/open-webui
version: 0.2

Blocks prompts containing Czech or English swear words and shows a
user-friendly bilingual message in the chat UI.

Detection runs in the inlet (before the request reaches the model) so
the message is always shown, regardless of how OpenWebUI handles the
empty choices array returned by the TrustyAI guardrails gateway.

The TrustyAI gateway provides a second enforcement layer at the network
level — this filter provides the user-facing UX.

To install: Admin Panel → Functions → + → paste this file → enable globally.
Messages are configurable via Valves (gear icon on the function card).
"""

import re
from pydantic import BaseModel, Field
from typing import Optional


# 127 Czech and English swear words — mirrors the TrustyAI gateway regex
_WORDS = [
    # English - fuck root
    "fuck", "fucking", "fucker", "fucked", "fuckface", "fuckwit", "fuckhead",
    "motherfucker", "motherfucking", "clusterfuck",
    # English - shit root
    "shit", "shitty", "shithead", "bullshit", "horseshit", "dipshit", "shitstorm",
    # English - genitalia / sexual
    "cunt", "cock", "cockhead", "dick", "dickhead", "pussy", "twat", "prick",
    "tits", "titties", "asshole", "asswipe", "arse", "arsehole", "wank", "wanker",
    "blowjob", "handjob", "cum", "jizz",
    # English - insults
    "bitch", "bastard", "tosser", "bollocks", "bugger", "douchebag", "jackass",
    "dumbass", "scumbag", "dirtbag", "asshat", "numbnuts", "fag", "slut", "whore", "skank",
    # English - slurs
    "faggot", "retard", "nigger", "nigga", "kike", "chink", "spic", "gook", "wetback", "dyke", "coon",
    # English - exploitation
    "rape", "pedophile", "pedo", "pedophilia", "perv",
    # Czech - kurv root
    "kurva", "kurvit", "zkurvit", "zkurvenej", "zkurvená", "vykurvenej", "nakurvit", "nakurvenej",
    # Czech - píč root (with and without diacritics)
    "píča", "pica", "píčovina", "picovina", "píčovitý", "picovity",
    # Czech - kund root
    "kunda", "kundička",
    # Czech - kokot root
    "kokot", "kokotina",
    # Czech - čurák (with and without diacritics)
    "čurák", "čůrák", "curak", "cuurak",
    # Czech - jeb root
    "jebat", "jebem", "najebaný", "vyjebaný", "pojebaný", "zajebaný", "jebnutý", "ujebat",
    # Czech - hov root
    "hovno", "hovado", "hovadina", "hovnivál",
    # Czech - insults
    "hajzl", "zmrd", "zmetek", "kretén", "kreten", "pako", "sráč", "srac",
    # Czech - gendered insults
    "mrcha", "děvka", "devka", "šlapka", "slapka", "svině", "svine",
    # Czech - slurs (all genders)
    "buzna", "buzerant", "buzerantka", "teplouš", "teplous", "lesba", "čubka", "cubka", "fena",
]

_PATTERN = re.compile(
    r"(?i)(" + "|".join(re.escape(w) for w in _WORDS) + r")"
)


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
        # Check the last user message for swear words before sending to the model.
        # Raising an exception surfaces the message in the OpenWebUI chat UI.
        messages = body.get("messages", [])
        user_messages = [m for m in messages if m.get("role") == "user"]
        if user_messages:
            content = user_messages[-1].get("content", "")
            if isinstance(content, str) and _PATTERN.search(content):
                raise Exception(
                    f"{self.valves.message_en}\n\n{self.valves.message_cs}"
                )
        return body

    def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        # Fallback: catch blocked responses from the TrustyAI gateway
        # (choices: [] with UNSUITABLE_INPUT warning) in case inlet was bypassed.
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
