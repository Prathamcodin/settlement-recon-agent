"""
LLM backend abstraction — the pluggability seam.

Everything above the backend (the fair prompt, the escalation-context
builder, the record/replay mechanics, the guard, the scorer) is
provider-agnostic. To swap providers — Gemini for Claude, Claude for
Llama, a self-hosted model, whatever — implement the LLMBackend
Protocol and pass an instance in.

The Protocol carries the minimum interface the classifier needs:
  - a `name` (goes into the evidence record for provenance)
  - a `system_prompt` (goes into the evidence record verbatim)
  - a `classify(user=..., max_tokens=...) -> str` that returns the
    raw response text, exactly as the provider produced it

Kept small on purpose: any streaming, retry, tool-use, or logging
behavior is a provider-level concern, not a classifier concern.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMBackend(Protocol):
    """Minimal LLM interface. See module docstring."""
    name: str
    system_prompt: str

    def classify(self, *, user: str, max_tokens: int) -> str:
        """Return the raw response text for the given user prompt."""
        ...


class GeminiBackend:
    """
    Google Gemini backend (google-genai SDK, the currently supported one).

    Default model is gemini-3.6-flash, the current free-tier flash model
    on Google AI Studio (gemini-2.5-flash was retired for new users).
    response_mime_type is set to application/json so the provider enforces
    JSON output at the API level — this matches the strict-JSON contract
    already stated in the prompt without leading the model toward any
    particular answer.
    """

    DEFAULT_MODEL_ID = "gemini-3.6-flash"

    def __init__(self, api_key: str, system_prompt: str,
                 model_id: str = DEFAULT_MODEL_ID):
        try:
            from google import genai
        except ImportError as e:
            raise ImportError(
                "GeminiBackend needs the google-genai package. "
                "Install with: pip install -r requirements.txt"
            ) from e
        self.name = model_id
        self.system_prompt = system_prompt
        self._model_id = model_id
        self._client = genai.Client(api_key=api_key)

    def classify(self, *, user: str, max_tokens: int) -> str:
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=self.system_prompt,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
        )
        resp = self._client.models.generate_content(
            model=self._model_id,
            contents=user,
            config=config,
        )
        text = resp.text
        if not text:
            # Surface the finish_reason so callers know whether this was
            # safety-blocking, truncation, or something else. We want the
            # unusual outcome recorded, not masked.
            finish_reason = None
            try:
                finish_reason = resp.candidates[0].finish_reason.name
            except Exception:
                pass
            raise RuntimeError(
                f"Gemini returned empty text (finish_reason={finish_reason})"
            )
        return text
