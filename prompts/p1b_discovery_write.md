# Discovery — WRITE PHASE (Step 1.1b)

All files have been read. Write `project_index.json` then `context.json` using `write_file`.

## RULES
- Write PURE JSON — no `//` or `/* */` comments
- **File paths in `project_index.json` must be ONLY paths from the "VALID FILE PATHS ONLY" list above** — no exceptions
- Do NOT invent paths, classes, or patterns not seen in actual files
- Do NOT call `read_file` again unless a single precise field is missing
- One `write_file` call per response; write project_index.json first

## VALID FILES
Only include in `project_index.json → services.*.files` the paths listed under "VALID FILE PATHS ONLY" in the user message. If a file is not in that list, it MUST NOT appear in project_index.json. Validation will reject any invented path.

## project_index.json — required fields
```json
{
  "project_type": "single|monorepo",
  "services": {
    "main": {
      "path": ".", "language": "python", "framework": "none",
      "entry_point": "main.py", "dev_command": "python main.py",
      "test_command": "pytest", "key_directories": ["core/", "web/"]
    }
  },
  "infrastructure": { "docker": false, "database": "none", "has_tests": true, "has_ci": false },
  "conventions": { "linter": "ruff", "formatter": "black", "import_style": "absolute", "naming_style": "snake_case" },
  "dependencies": ["eel"],
  "discovered_at": "2025-01-01T00:00:00"
}
```

## context.json — required fields
```json
{
  "task_relevant_files": {
    "to_modify": ["core/state.py"],
    "to_create": ["core/new_feature.py"],
    "to_reference": ["core/phases/coding.py"]
  },
  "existing_patterns": {
    "eel_pattern": "@eel.expose decorates Python functions callable from JS"
  },
  "design_system": "CSS vars in use: --bg, --accent, --text, --r6 (copy from CSS DESIGN TOKENS above)",
  "existing_implementations": [
    { "description": "Pattern in file", "file": "core/state.py", "relevant_because": "Shows how to..." }
  ],
  "tech_notes": ["Key tech notes about the project"],
  "files_read": ["core/state.py", "main.py"]
}
```

## CRITICAL
- `to_reference` must have ≥ entries as `to_modify`
- For every file in `to_modify`: include its imports and reverse imports in `to_reference`
- Use EXACT field names above — wrong names cause validation failure
- After writing BOTH files, call `confirm_phase_done` to finish
