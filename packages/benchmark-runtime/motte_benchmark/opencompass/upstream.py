"""Importable OpenCompass 0.4.2 bridge types; loaded only in the runner venv.

Keep secrets out of MMEngine Config: it serializes both the parent config and
per-task configs. Resolve environment references only while constructing models.
"""
from __future__ import annotations

import json
import os

from datasets import Dataset
from opencompass.datasets.base import BaseDataset
from opencompass.models import OpenAI


class LocalMCQDataset(BaseDataset):
    """The selected frozen prompts already contain the selected few-shot rows.

    ZeroRetriever intentionally performs no second selection. Each prompt passes
    through the upstream PromptTemplate/GenInferencer unchanged, in frozen order.
    Target gold is deliberately absent; platform scoring owns that boundary.
    """

    @staticmethod
    def load(path):
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        return Dataset.from_list(rows)


class RuntimeOpenAI(OpenAI):
    """Resolve references after config serialization, never in config globals."""

    def __init__(self, *, key_env="OPENAI_API_KEY", base_url_env=None, **kwargs):
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f"runner credential environment is missing: {key_env}")
        if base_url_env is not None:
            base = os.environ.get(base_url_env, "").rstrip("/")
            if not base:
                raise ValueError(f"runner endpoint environment is missing: {base_url_env}")
            # Platform references name the OpenAI-compatible API base. Upstream
            # OpenAI expects the full chat-completions URL instead.
            kwargs["openai_api_base"] = (
                base if base.endswith("/chat/completions") else base + "/chat/completions"
            )
        super().__init__(key=key, **kwargs)
