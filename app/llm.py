"""Single call site for the model.

Deliberately provider-agnostic. Razorpay's own Agent Studio is built on the Claude Agent
SDK, so parity with their stack is worth keeping reachable in the pitch, but this build
runs on Google AI Studio. Swapping providers should touch this file and nothing else,
which is why no other module imports google.genai.

Model availability is not assumed. Measured on 2026-08-26, gemini-3.7-flash returned 503
on two of three consecutive calls while gemini-3.6-flash answered first time, and the
SDK's own retry can hang for minutes before surfacing the error. A recovery agent that
stalls because its own model is busy is a bad demo and a worse product, so calls fail
over across a model chain on a bounded clock and the audit log records which model
actually answered.
"""

from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from app import audit, config

# Transient upstream conditions worth trying the next model for. A 400 or a 404 is a bug
# in the request or a dead model name, and retrying either just wastes the clock.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GOOGLE_API_KEY:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Create one at https://aistudio.google.com/apikey "
                "and add it to .env."
            )
        _client = genai.Client(
            api_key=config.GOOGLE_API_KEY,
            http_options=types.HttpOptions(
                timeout=config.LLM_TIMEOUT_MS,
                # One attempt per model. Retrying a busy model in place costs the demo
                # clock; the next model in the chain is the faster recovery.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def model_chain() -> list[str]:
    """Primary model first, then fallbacks, with duplicates removed."""
    chain = [config.LLM_MODEL, *config.LLM_FALLBACK_MODELS]
    return list(dict.fromkeys(name for name in chain if name))


def complete(
    prompt: str,
    *,
    system: str | None = None,
    response_schema: Any | None = None,
    temperature: float = 0.2,
) -> str:
    """Return the model's text response, failing over across the model chain.

    Passing `response_schema` constrains the model to JSON matching that schema, which is
    how the recovery strategist returns a decision object rather than prose.

    Never returns an empty string. A model that answers with nothing has failed, and the
    caller cannot tell that apart from a model that answered badly, so the failover is
    made here rather than guessed at upstream.
    """
    generation_config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
    )
    if response_schema is not None:
        generation_config.response_mime_type = "application/json"
        generation_config.response_schema = response_schema

    chain = model_chain()
    failures: list[str] = []

    for index, model in enumerate(chain):
        try:
            response = _get_client().models.generate_content(
                model=model, contents=prompt, config=generation_config
            )
        except (errors.APIError, httpx.TimeoutException, httpx.TransportError) as exc:
            # A client-side read timeout is not an APIError and would otherwise skip the
            # whole chain, which is the failure this fallback exists to survive.
            status = getattr(exc, "code", None) or type(exc).__name__
            failures.append(f"{model}:{status}")
            retryable = status in RETRYABLE_STATUS or isinstance(
                exc, httpx.TimeoutException | httpx.TransportError
            )
            if retryable and index < len(chain) - 1:
                continue
            # Only a retryable failure on the last model has exhausted anything. A 400 or a
            # 404 stops on the first model by design, and calling that "exhausted" told the
            # audit log four models had been tried when none had. The log is the evidence
            # for the failover story, so it has to say which of the two happened.
            audit.record(
                "llm.exhausted" if retryable else "llm.unrecoverable",
                chain=chain,
                failures=failures,
            )
            raise

        # An empty body is a failure, not an answer. Every call here passes a
        # response_schema, so no request has "no text" as its correct reply: a blocked
        # candidate, a safety stop and a truncated stream all arrive as response.text of
        # None, with no exception raised. Returning "" handed the caller something only it
        # could misinterpret, and the chain that exists for exactly this never fired.
        text = response.text or ""
        if not text.strip():
            failures.append(f"{model}:empty_response")
            if index < len(chain) - 1:
                continue
            audit.record("llm.exhausted", chain=chain, failures=failures)
            raise RuntimeError(
                f"every model in {chain} returned an empty response: {failures}"
            )

        if index > 0:
            # Evidence for the failure-recovery story: the agent kept working through a
            # provider outage rather than stalling.
            audit.record(
                "llm.failover", served_by=model, skipped=failures, primary=chain[0]
            )
        return text

    raise RuntimeError(f"no model in {chain} could be reached")


def available_models() -> list[str]:
    """List model ids this key can reach.

    Note that appearing here does not guarantee generateContent works: gemini-2.5-flash
    is listed by this endpoint and returns 404 when called.
    """
    return [m.name for m in _get_client().models.list()]
