"""OpenCode Go coding-client identity, kept outside the model request body.

https://opencode.ai/docs/go/#where-can-i-use-it (checked 2026-09-30).
Session identity belongs to the conversation, never the shared provider instance.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

GO_BASE_URL = "https://opencode.ai/zen/go/v1"
SPACE_BUNNY_MODEL = "space-bunny-free"
USER_AGENT = "MoTTEavl/0.1.0"


def conversation_headers(base_url: str, metadata: dict[str, Any]) -> dict[str, str]:
    url = urlsplit(base_url)
    if url.hostname != "opencode.ai" or not url.path.rstrip("/").startswith("/zen/go/"):
        return {}
    session = metadata.get("session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", session):
        raise ValueError("OpenCode Go requires a stable conversation metadata.session_id")
    return {"User-Agent": USER_AGENT, "x-opencode-session": session}
