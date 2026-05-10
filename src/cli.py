"""
Art Hunter — CLI wrapper.
Calls claude via subprocess only. NO import anthropic, NO SDK.
"""

import subprocess
import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def claude_call(
    prompt: str,
    system_prompt: str,
    model: str,
    web_search: bool = False,
    step_name: str = "unknown",
) -> str:
    """
    Call Claude CLI via subprocess and return the text result.
    Logs every call to logs/calls_YYYYMMDD.jsonl.
    Raises RuntimeError on non-zero exit or empty response.
    """
    # On Windows, claude is a .cmd file — must be called via cmd /c
    cmd = [
        "cmd", "/c", "claude",
        "--print",
        "--model", model,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
    ]

    if web_search:
        # --tools enables the built-in WebSearch/WebFetch tools
        cmd += ["--tools", "WebSearch,WebFetch"]
    else:
        cmd += ["--tools", ""]

    start = datetime.now()

    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    elapsed_ms = int((datetime.now() - start).total_seconds() * 1000)

    if proc.returncode != 0:
        err = (proc.stderr or "unknown error")[:500]
        _log(step_name, model, prompt, "", elapsed_ms, error=err)
        raise RuntimeError(f"[{step_name}] claude exit {proc.returncode}: {err}")

    raw = proc.stdout.strip()
    if not raw:
        _log(step_name, model, prompt, "", elapsed_ms, error="empty stdout")
        raise RuntimeError(f"[{step_name}] claude returned empty output")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # plain-text fallback (shouldn't happen with --output-format json)
        _log(step_name, model, prompt, raw, elapsed_ms)
        return raw

    text = data.get("result", "")
    cost = data.get("cost_usd", 0.0)
    duration = data.get("duration_ms", elapsed_ms)
    usage = data.get("usage", {})

    _log(step_name, model, prompt, text, duration, cost=cost, usage=usage)
    return text


def _log(
    step: str,
    model: str,
    prompt: str,
    response: str,
    duration_ms: int,
    cost: float = 0.0,
    usage: dict = None,
    error: str = None,
):
    today = datetime.now().strftime("%Y%m%d")
    log_file = LOGS_DIR / f"calls_{today}.jsonl"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "step": step,
        "model": model,
        "duration_ms": duration_ms,
        "cost_usd": cost,
        "usage": usage or {},
        "prompt_preview": prompt[:300] + "…" if len(prompt) > 300 else prompt,
        "response_preview": response[:600] + "…" if len(response) > 600 else response,
    }
    if error:
        entry["error"] = error

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
