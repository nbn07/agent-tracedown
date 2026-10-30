"""Run τ-bench's run.py with one compatibility shim: newer Anthropic models reject
``temperature``, which τ-bench always sends. Run from inside the τ-bench checkout."""
import runpy
import sys
import time

import litellm

_completion = litellm.completion


def completion(*args, **kwargs):
    model = str(kwargs.get("model", ""))
    provider = str(kwargs.get("custom_llm_provider", ""))
    if "anthropic" in provider or "claude" in model:
        kwargs.pop("temperature", None)
        kwargs.setdefault("max_tokens", 8000)  # leave room for the model's own thinking
    # A rate-limited call must not turn into an agent "failure": wait and retry.
    for attempt in range(8):
        try:
            return _completion(*args, **kwargs)
        except litellm.RateLimitError:
            time.sleep(min(60, 5 * 2 ** attempt))
    return _completion(*args, **kwargs)


litellm.completion = completion  # τ-bench does `from litellm import completion` at import time
sys.argv = ["run.py"] + sys.argv[1:]
runpy.run_path("run.py", run_name="__main__")
