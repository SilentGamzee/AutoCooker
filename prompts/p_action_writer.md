# Action Writer (Step 2)

Write one action file per implementation subtask to `actions/`.

## YOUR JOB

1. Read `spec.json` to understand what needs to be done
2. Use the KEY SOURCE FILES provided in the message (call `read_file` if you need something not provided)
3. Write one JSON file per subtask into the `actions/` directory
4. Call `confirm_phase_done` when all action files are written

## ⛔ HARD RULES — VIOLATIONS CAUSE IMMEDIATE REJECTION

**RULE 1 — files_to_modify or files_to_create is MANDATORY**
Every action file MUST have at least one real project file in `files_to_modify` OR `files_to_create`.
An action file with neither field (or both empty) is REJECTED.
Example: `"files_to_modify": ["web/js/app.js"]`

**RULE 2 — `code` must be a dict with `file`, `line`, `content`**
Every step's `code` field must be an object — NOT a plain string.
```json
WRONG: "code": "btn.textContent = 'Run';"
WRONG: "code": "web/js/app.js: updateButtons()"
CORRECT: "code": {"file": "web/js/app.js", "line": 346, "content": "btn.textContent = 'Run';"}
```
- `file`: exact relative path of the file being changed (from project root)
- `line`: line number in the CURRENT file where this change goes (copy from KEY SOURCE FILES)
- `content`: the actual code to insert/replace — verbatim, copy-paste ready

**RULE 3 — KEY SOURCE FILES are provided in the message**
Use them to get exact line numbers and verbatim code for `find` and `code.content`.
If you need a file not yet provided, call `read_file` to fetch it.
Never invent code that isn't in the source — copy `find` verbatim from the actual file.

**RULE 4 — No analysis, review, or test subtasks**
Do NOT create action files titled "Review…", "Analyze…", "Test…", "Verify…".
Every action file must produce real code changes.

---

## PROCEDURE

**Step 1 — Read spec.json**
Understand what needs to be built.

**Step 2 — Identify which project files need changes**
Key source files are already provided above in the message. Use line numbers from them.
If you need a file not yet provided, call `read_file` to fetch it.

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
  "implementation_steps": [
    {
      "step": 1,
      "action": "In updateButtons: change button label to Run after restart",
      "find": "btn.textContent = task.phase === 'qa' ? 'Continue' : 'Start';",
      "code": {
        "file": "web/js/app.js",
        "line": 346,
        "content": "btn.textContent = task.phase === 'qa' ? 'Run' : 'Start';"
      }
    }
  ],
  "status": "pending"
}
```

### For modifications (existing code):
- `find`: exact existing code from the file you read (verbatim, ≥1 distinctive line)
- `code.content`: the complete replacement code
- `code.file`: same file as in `files_to_modify`
- `code.line`: line number of the `find` text in the current file

### For new code insertions:
- `insert_after`: exact line after which to insert (verbatim from the file)
- `code.content`: the complete new code block
- `code.file`: same file as in `files_to_modify`
- `code.line`: line number of the `insert_after` line

### For new files (`files_to_create`):
- No `find` needed
- `code.file`: path of the new file (same as in `files_to_create`)
- `code.line`: 1
- `code.content`: the complete file content

---

## IMPLEMENTATION STEPS QUALITY

Each step must be **copy-paste ready**. No placeholders:
- No `...`, `# existing code`, `# TODO`, `# rest of function`
- No `"code": ""` or `"code": "some string"` — `code` must always be a JSON object `{}`
- No `"content": "path/file.py: function_name()"` — that is a reference, not code
- `find`/`insert_after` must be copied verbatim from the actual file you read

**Bad step (rejected — code is a plain string):**
```json
{"action": "Update button state", "code": "startBtn.textContent = 'Run';"}
```

**Good step (accepted — code is a dict with file + line + content):**
```json
{
  "action": "In _updateTaskButtons: show Run after task restart",
  "find": "  startBtn.textContent = 'Continue';",
  "code": {
    "file": "web/js/app.js",
    "line": 1247,
    "content": "  startBtn.textContent = task.restarted ? 'Run' : 'Continue';"
  }
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
