"""
openai_runner.py — OpenAI API interface for Shepdog scenarios

Wraps the OpenAI chat completions API and returns results in the same dict
structure as model_runner, with added cost tracking fields.

Public API
──────────
  run_openai_prompt(model, system_prompt, user_prompt) → dict
"""

import os
import time

# Update these if OpenAI changes pricing (prices are per million tokens)
PRICING = {
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},   # per million tokens
    "gpt-5.4-mini": {"input": 1.10, "output": 4.40},   # per million tokens — update if different
}

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from model_runner import extract_completion_claim


def run_openai_prompt(model: str, system_prompt: str, user_prompt: str) -> dict:
    """
    Call OpenAI chat completions API. Returns a dict compatible with
    model_runner.run_prompt() results but with added cost fields.

    Return structure:
        {
            "model":         str,
            "response":      str,
            "duration_ms":   int,
            "tool_claims":   list,
            "error":         None or str,
            "cost_usd":      float,
            "input_tokens":  int,
            "output_tokens": int,
        }
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return _error_result(model, "OPENAI_API_KEY not set")

    try:
        import openai
    except ImportError:
        return _error_result(model, "openai package not installed — run: pip install openai")

    client = openai.OpenAI(api_key=api_key)
    t_start = time.time()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        )
        duration_ms = round((time.time() - t_start) * 1000)

        text  = response.choices[0].message.content or ""
        usage = response.usage

        input_tokens  = usage.prompt_tokens     if usage else 0
        output_tokens = usage.completion_tokens if usage else 0

        pricing  = PRICING.get(model, {"input": 0.0, "output": 0.0})
        cost_usd = (
            (input_tokens  / 1e6 * pricing["input"]) +
            (output_tokens / 1e6 * pricing["output"])
        )

        _, claimed_count = extract_completion_claim(text)
        tool_claims = [{"claimed_count": claimed_count}] if claimed_count else []

        return {
            "model":         model,
            "response":      text,
            "duration_ms":   duration_ms,
            "tool_claims":   tool_claims,
            "error":         None,
            "cost_usd":      round(cost_usd, 8),
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
        }

    except Exception as exc:
        duration_ms = round((time.time() - t_start) * 1000)
        return {
            "model":         model,
            "response":      "",
            "duration_ms":   duration_ms,
            "tool_claims":   [],
            "error":         str(exc),
            "cost_usd":      0.0,
            "input_tokens":  0,
            "output_tokens": 0,
        }


def _error_result(model: str, error: str) -> dict:
    return {
        "model":         model,
        "response":      "",
        "duration_ms":   0,
        "tool_claims":   [],
        "error":         error,
        "cost_usd":      0.0,
        "input_tokens":  0,
        "output_tokens": 0,
    }
