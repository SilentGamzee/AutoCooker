# Action Writer (Step 2)

Write one action file per implementation subtask to `actions/`.

## YOUR JOB

1. Read `spec.json` to understand what needs to be done
2. Read the relevant **project source files** to understand existing code
3. Write one JSON file per subtask into the `actions/` directory
4. Call `confirm_phase_done` when all action files are written

## ⛔ HARD RULES — VIOLATIONS CAUSE IMMEDIATE REJECTION

**RULE 1 — files_to_modify or files_to_create is MANDATORY**
Every action file MUST have at least one real project file in `files_to_modify` OR `files_to_create`.
An action file with neither field (or both empty) is REJECTED.
Example: `"files_to_modify": ["web/js/app.js"]`

**RULE 2 — `code` must be real implementation code, not a reference**
WRONG: `"code": "web/js/app.js: updateButtons()"` ← file path reference, REJECTED
WRONG: `"code": "core/state.py: clear_task_state()"` ← function reference, REJECTED
CORRECT: `"code": "function updateButtons(task) {\n  btn.textContent = 'Run';\n}"` ← real code

**RULE 3 — Read source files BEFORE writing action files**
You must call `read_file` or `read_files_batch` on every file you plan to modify.
Without reading, you cannot write correct `find`/`insert_after` anchors.

**RULE 4 — No analysis, review, or test subtasks**
Do NOT create action files titled "Review…", "Analyze…", "Test…", "Verify…".
Every action file must produce real code changes.

---

## PROCEDURE

**Step 1 — Read spec.json**
Understand what needs to be built.

**Step 2 — Identify which project files need changes**
From the spec and project file list, determine exactly which files to modify.
Read each of them with `read_file`.

**Step 3 — For each file that needs changes, create one action file**
One file changed = one action file. Group small related changes to the same file together.

**Step 4 — Write action files, then call confirm_phase_done**

---

## ACTION FILE FORMAT

```json
{
  "id": "T-001",
  "title": "Short imperative title: what this action does",
  "description": "1-2 sentences: what changes and why.",
  "files_to_create": [],
  "files_to_modify": ["web/js/app.js"],
  "patterns_from": ["web/js/app.js"],
  "completion_without_ollama": "web/js/app.js contains 'Run'",
  "implementation_steps": [
    {
      "step": 1,
      "action": "In updateButtons: change button label to Run after restart",
      "find": "btn.textContent = task.phase === 'qa' ? 'Continue' : 'Start';",
      "replace": "btn.textContent = task.phase === 'qa' ? 'Run' : 'Start';",
      "code": "btn.textContent = task.phase === 'qa' ? 'Run' : 'Start';"
    }
  ],
  "status": "pending"
}
```

### For modifications (existing code):
- `find`: exact existing code from the file you read (verbatim, ≥1 distinctive line)
- `replace`: the complete replacement code
- `code`: same as `replace`

### For new code additions:
- `insert_after`: exact line after which to insert (verbatim from the file)
- `code`: the complete new code block

### For new files (`files_to_create`):
- `code`: the complete file content

---

## IMPLEMENTATION STEPS QUALITY

Each step must be **copy-paste ready**. No placeholders:
- No `...`, `# existing code`, `# TODO`, `# rest of function`
- No `"code": ""` — empty code field is rejected
- No `"code": "path/file.py: function_name()"` — that is a reference, not code
- `find`/`insert_after` must be copied verbatim from the actual file you read

**Bad step (rejected):**
```json
{"action": "Update button state", "code": "web/js/app.js: updateButtonState()"}
```

**Good step (accepted):**
```json
{
  "action": "In _updateTaskButtons: show Run after task restart",
  "find": "  startBtn.textContent = 'Continue';",
  "replace": "  startBtn.textContent = task.restarted ? 'Run' : 'Continue';",
  "code": "  startBtn.textContent = task.restarted ? 'Run' : 'Continue';"
}
```

---

## ORDERING
- Data/state changes first (T001)
- Backend/API changes next
- HTML before JS that references new elements
- CSS last

## FINISH
After writing ALL action files, call `confirm_phase_done`.
