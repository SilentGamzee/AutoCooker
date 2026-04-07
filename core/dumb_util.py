import os
from core.state import AppState

def get_dumb_task_workdir_diff(state:AppState, task_id: str) -> dict:
    """Return unified diff between workdir files and project files."""
    task = state.get_task(task_id)
    if not task or not task.task_dir:
        return {"ok": False, "error": "Task not found"}

    from core.sandbox import WORKDIR_NAME
    workdir = os.path.join(task.task_dir, WORKDIR_NAME)
    project = task.project_path or state.working_dir

    if not os.path.isdir(workdir):
        return {"ok": False, "error": "Workdir not found — coding phase not run yet"}

    _SKIP = (
        "__pycache__", ".pyc", ".pyo", ".pyd", ".git",
        "node_modules", ".egg-info", ".dist-info",
        ".mypy_cache", ".ruff_cache", ".pytest_cache",
    )

    diffs = []
    for dirpath, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not any(pat in d for pat in _SKIP)]
        for fname in files:
            if any(pat in fname for pat in _SKIP):
                continue
            wfile = os.path.join(dirpath, fname)
            rel   = os.path.relpath(wfile, workdir).replace("\\", "/")
            pfile = os.path.join(project, rel)

            try:
                wtext = open(wfile, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                continue

            if os.path.isfile(pfile):
                try:
                    ptext = open(pfile, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    ptext = ""
                if wtext == ptext:
                    continue   # identical — skip
                label = f"modified: {rel}"
            else:
                ptext = ""
                label = f"new file: {rel}"

            import difflib
            diff_lines = list(difflib.unified_diff(
                ptext.splitlines(keepends=True),
                wtext.splitlines(keepends=True),
                fromfile=f"project/{rel}",
                tofile=f"workdir/{rel}",
                lineterm="",
            ))
            diffs.append({
                "rel":   rel,
                "label": label,
                "diff":  "".join(diff_lines)[:8000],
            })

    return {
        "ok": True,
        "files": diffs,
        "total": len(diffs),
    }