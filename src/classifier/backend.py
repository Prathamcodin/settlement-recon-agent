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
    Google Gemini backend (google-generativeai SDK).

    Default model is gemini-2.5-flash, available on Google's free tier.
    The system prompt is baked into the underlying GenerativeModel at
    construction (Gemini takes system_instruction at model-init, not
    per-call). response_mime_type is set to application/json so the
    provider enforces JSON output at the API level — matches the
    strict-JSON contract already in the prompt.
    """

    DEFAULT_MODEL_ID = "gemini-2.5-flash"

    def __init__(self, api_key: str, system_prompt: str,
                 model_id: str = DEFAULT_MODEL_ID):
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ImportError(
                "GeminiBackend needs the google-generativeai package. "
                "Install with: pip install -r requirements.txt"
            ) from e
        genai.configure(api_key=api_key)
        self.name = model_id
        self.system_prompt = system_prompt
        self._model = genai.GenerativeModel(
            model_id,
            system_instruction=system_prompt,
        )

    def classify(self, *, user: str, max_tokens: int) -> str:
        resp = self._model.generate_content(
            user,
            generation_config={
                "max_output_tokens": max_tokens,
                "response_mime_type": "application/json",
            },
        )
        try:
            return resp.text
        except (ValueError, AttributeError) as e:
            # No usable text — surface the finish_reason so callers know
            # whether this was safety-blocking, truncation, or something else.
            finish_reason = None
            try:
                finish_reason = resp.candidates[0].finish_reason.name
            except Exception:
                pass
            raise RuntimeError(
                f"Gemini returned no usable text (finish_reason={finish_reason})"
            ) from e
