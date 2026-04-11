# Discovery — WRITE PHASE (Step 1.1b)

All files have been read. Write `project_index.json` then `context.json` using `write_file`.

## RULES
- Write PURE JSON — no `//` or `/* */` comments
- **File paths in `project_index.json` must be ONLY paths from the "VALID FILE PATHS ONLY" list above** — no exceptions
- Do NOT invent paths, classes, or patterns not seen in actual files
- Do NOT call `read_file` again unless a single precise field is missing
- One `write_file` call per response; write project_index.json first

## VALID FILES
Only include in `project_index.json → files` the paths listed under "VALID FILE PATHS ONLY" in the user message. If a file is not in that list, it MUST NOT appear in project_index.json. Validation will reject any invented path.

## project_index.json — required format
```json
{
  "files": {
    "core/state.py": {
      "description": "AppState and KanbanTask dataclass; manages board state and task fields",
      "symbols": ["AppState", "KanbanTask", "save_kanban"],
      "language": "python"
    },
    "web/js/app.js": {
      "description": "Frontend logic; _updateTaskButtons controls Restart/Continue button visibility",
      "symbols": ["_updateTaskButtons", "restartActiveTask", "btn-continue", "btn-restart"],
      "language": "javascript"
    }
  }
}
```
Rules:
- `files` is a flat dict: path → {description, symbols, language}
- Include ONLY files from the "VALID FILE PATHS ONLY" list
- `symbols` must list actual function/class/element names found in the file
- `description` must describe what the file does relevant to THIS task (not generic)
- Do NOT use a `services` wrapper

## context.json — required fields
```json
{
  "task_relevant_files": {
    "to_modify": [
      {"path": "web/js/app.js", "reason": "_updateTaskButtons hasStarted logic needs fix"}
    ],
    "to_create": [],
    "to_reference": [
      {"path": "core/state.py", "reason": "KanbanTask.column field drives board column assignment"}
    ]
  },
  "patterns": {
    "eel_pattern": "@eel.expose in main.py exposes Python to JS as eel.methodName()()"
  },
  "design_tokens": "var(--bg), var(--accent), var(--r6) — copy from CSS DESIGN TOKENS above",
  "files_read": ["core/state.py", "web/js/app.js"]
}
```

## CRITICAL
- `to_reference` must have ≥ entries as `to_modify`
- For every file in `to_modify`: include its imports and reverse imports in `to_reference`
- Use EXACT field names above — wrong names cause validation failure
- After writing BOTH files, call `confirm_phase_done` to finish
