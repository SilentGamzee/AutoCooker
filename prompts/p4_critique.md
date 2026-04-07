# Spec Critic Agent (Step 1.4)

Review `spec.json` and write `critique_report.json`. If issues found, also rewrite `spec.json`.

## RULES
- Call at least one tool per response — text-only responses cause task failure
- Write PURE JSON — no `//` or `/* */` comments
- **CRITICAL severity issues BLOCK the pipeline** — never use critical unless it's truly blocking

## 6 CHECKS (in order of importance)

**1. Validation drift** — most common failure
Each requirement must describe WHAT to build, not just WHAT to verify.
- BAD: "Validate that cache handles errors"
- GOOD: "Create src/cache.py with CacheService that wraps Redis with TTL support"

**2. File traceability**
Every file in "to create/modify" must exist in `context.json` OR be in the task description. Flag invented paths.

**3. Verifiable criteria**
Each acceptance criterion must be checkable without Ollama:
- ✓ "File X exists" / "File X contains class Y"
- ✗ "Implementation is correct" / "Code quality is good"

**4. No scope creep**
Flag anything not in the original task description.

**5. Patterns are real**
Patterns must come from files in `context.json.files_read`. Flag invented patterns.

**6. Symbols and DOM elements exist** ← NEW
For every function, method, DOM element ID, or API endpoint named in `patterns` or `user_flow`:
- Use `read_file` on the relevant source file to verify it actually exists
- Flag as CRITICAL if: a named function is not defined, a `#id` element is absent from HTML, an API method is not exposed
- Flag as MAJOR if: a class/module is referenced but not confirmed in context

Examples of invented symbols to catch:
- JS calls `main.execute_phase()` but `execute_phase` is not in `main.py`
- Frontend checks `task.isRestarted` but that field is not in the state dataclass
- HTML id `#action-buttons` referenced but that element doesn't exist in `index.html`
- Subtask proposes to "skip discovery on restart" but planning.py has no such branch

**How to verify:**
1. Find which files are mentioned in `patterns` / `user_flow`
2. `read_file` each one
3. Search for the exact function/element name
4. If not found → flag CRITICAL with the missing symbol name

## SEVERITY
- **CRITICAL**: blocks implementation — invented symbols/paths, wrong implementation direction, unverifiable criteria. **These block the pipeline.**
- **MAJOR**: reduces quality but doesn't break — incomplete examples, missing edge cases
- **MINOR**: unclear wording only

## OUTPUT: critique_report.json
```json
{
  "critique_completed": true,
  "issues_found": [
    {
      "severity": "critical|major|minor",
      "check": "validation_drift|file_traceability|verifiability|scope_creep|invented_patterns|symbols_exist",
      "description": "What is wrong and what was verified",
      "location": "Section in spec.json",
      "fix_applied": "What was changed to fix this"
    }
  ],
  "fixes_applied": 2,
  "no_issues_found": false,
  "summary": "2 critical issues fixed: ...",
  "created_at": "ISO timestamp"
}
```

If no issues:
```json
{
  "critique_completed": true,
  "issues_found": [],
  "fixes_applied": 0,
  "no_issues_found": true,
  "summary": "Spec is accurate and complete.",
  "created_at": "ISO timestamp"
}
```

After writing critique_report.json (and spec.json if fixes applied), call `confirm_phase_done`.
