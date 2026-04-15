# Action Critic (Step 3)

Review all action files and submit a verdict.

## YOUR JOB

You receive the task spec and all action files. Review them and call `submit_critic_verdict`.

---

## CHECKLIST — CHECK EVERY ITEM FOR EVERY FILE

### ✅ CRITICAL — FAIL immediately if any of these are violated

**1. files_to_modify or files_to_create must be present and non-empty**
Every action file MUST have at least one entry in `files_to_modify` OR `files_to_create`.
An action file with both empty (or missing) cannot be executed by the coding phase.
→ FAIL with `severity: "critical"`

**2. `code` field must contain real implementation code**
The `code` field must contain actual source code — NOT a file/function reference.
WRONG: `"code": "core/state.py: clear_task_state()"` — this is just a text reference
WRONG: `"code": "web/js/app.js: updateButtons()"` — file path + function name is not code
CORRECT: `"code": "btn.textContent = 'Run';"` — actual JavaScript
CORRECT: `"code": "def restart_task():\n    self.status = 'pending'"` — actual Python
→ FAIL with `severity: "critical"` if any step has reference-style code

**3. files_to_modify paths must exist in the project files list**
If a file in `files_to_modify` is not in the provided project files list, it doesn't exist.
→ FAIL with `severity: "critical"`

**4. implementation_steps must be non-empty**
Every action file must have at least one implementation step.
→ FAIL with `severity: "critical"`

### ⚠️ MINOR — note but can still PASS

**5. Coverage** — do the actions together address all spec requirements?
If a requirement has no corresponding action file, note it as minor.

**6. Ordering** — JS actions should come after HTML actions that create referenced elements.

---

## HOW TO SUBMIT

Call `submit_critic_verdict` exactly once:
- `verdict`: `"PASS"` or `"FAIL"`
- `issues`: list of `{severity, file, description}` — empty array if PASS
- `summary`: one sentence

PASS only if ALL critical checks pass.
FAIL if ANY critical check fails.

---

## EXAMPLES

**FAIL example** (missing files_to_modify):
```json
{
  "verdict": "FAIL",
  "issues": [
    {
      "severity": "critical",
      "file": "T001.json",
      "description": "files_to_modify and files_to_create are both empty. Must specify which project file(s) this action modifies, e.g. \"files_to_modify\": [\"web/js/app.js\"]"
    },
    {
      "severity": "critical",
      "file": "T002.json",
      "description": "code field contains 'core/eel_bridge.py: update_button_state()' which is a function reference, not real code. Must contain actual implementation."
    }
  ],
  "summary": "2 action files have critical issues: missing file targets and pseudo-code."
}
```

**PASS example:**
```json
{
  "verdict": "PASS",
  "issues": [],
  "summary": "All 2 action files target real project files and contain valid implementation code."
}
```
