"""Session memory extraction.

Port of auto-claude-logic services/extractMemories. After a task finishes,
runs a single lightweight LLM call to summarize project-specific facts that
emerged during the run (architecture quirks, gotchas, undocumented invariants)
and appends them to a per-project memory file. Subsequent tasks read the file
during planning so we do not re-discover the same things.
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from typing import Optional

MEMORY_FILENAME = "_session_memory.md"
MAX_LOG_CHARS_FOR_EXTRACTION = 20_000

EXTRACTION_PROMPT = (
    "You are summarizing learnings from one finished AutoCooker task. "
    "Read the task title, description, and recent logs. Extract only "
    "PROJECT-SPECIFIC facts that would help a future task: undocumented "
    "invariants, architectural quirks, gotchas, file roles, naming "
    "conventions discovered, dead fields, runtime constraints.\n\n"
    "DO NOT include: this task's specific changes, fix details, "
    "subtask titles, debug noise, generic programming advice.\n\n"
    "Output 0-5 bullets. Each bullet ≤ 200 chars. If nothing worth "
    "recording, output exactly the literal token NONE and nothing else.\n\n"
    "Format:\n"
    "- <fact 1>\n"
    "- <fact 2>\n"
)


def _project_memory_path(project_root: str) -> str:
    base = os.path.join(project_root, ".tasks")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, MEMORY_FILENAME)


def load_session_memory(project_root: str) -> str:
    path = _project_memory_path(project_root)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _logs_to_text(logs: list[dict], limit: int = MAX_LOG_CHARS_FOR_EXTRACTION) -> str:
    out: list[str] = []
    for e in logs[-200:]:
        msg = e.get("msg", "")
        if not msg:
            continue
        out.append(f"[{e.get('phase','')}][{e.get('type','')}] {msg}")
    text = "\n".join(out)
    if len(text) > limit:
        text = text[-limit:]
    return text


def extract_and_append(
    *,
    project_root: str,
    task_title: str,
    task_description: str,
    logs: list[dict],
    ollama_client,
    model: str,
    log_fn=None,
) -> Optional[str]:
    """Run one LLM call to extract memories; append non-empty result to MEMORY file.

    Returns the appended block or None if nothing was added.
    """
    log_text = _logs_to_text(logs)
    if not log_text.strip():
        return None
    user_prompt = (
        f"{EXTRACTION_PROMPT}\n\n"
        f"=== TASK ===\n{task_title}\n\n{task_description}\n\n"
        f"=== LOGS (truncated) ===\n{log_text}\n"
    )
    try:
        resp = ollama_client.complete(
            model=model,
            prompt=user_prompt,
            max_tokens=400,
            log_fn=log_fn,
        )
    except Exception as e:
        if log_fn:
            log_fn(f"[memory] extraction failed: {e}", "warn")
        return None
    body = (resp or "").strip()
    if not body or body.upper().startswith("NONE"):
        if log_fn:
            log_fn("[memory] no facts extracted", "info")
        return None
    bullets = [ln for ln in body.splitlines() if ln.strip().startswith("-")]
    if not bullets:
        return None
    block = (
        f"\n## {datetime.utcnow().strftime('%Y-%m-%d')} — {task_title[:80]}\n"
        + "\n".join(bullets)
        + "\n"
    )
    path = _project_memory_path(project_root)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception as e:
        if log_fn:
            log_fn(f"[memory] write failed: {e}", "warn")
        return None
    if log_fn:
        log_fn(f"[memory] appended {len(bullets)} fact(s) to {MEMORY_FILENAME}", "ok")
    return block
