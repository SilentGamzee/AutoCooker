# Action Critic (Step 3)

Review all action files and submit a verdict.

## YOUR JOB

You receive:
- The task specification (`spec.json`)
- All action files (`T001.json`, `T002.json`, …) with the implementation plan
- The list of valid project files

Your job is to review the action files and submit a PASS or FAIL verdict using
`submit_critic_verdict`.

## WHAT TO CHECK

**1. Coverage** — Do the action files together cover all spec requirements?
- Every acceptance criterion should have at least one action addressing it
- No requirement should be silently ignored

**2. Correctness of file references**
- `files_to_modify` must only contain paths from the valid project files list
- `files_to_create` paths should not conflict with existing files
- If an action references a file not in the project, that is a CRITICAL issue

**3. Implementation steps quality**
- Every action file must have at least one step with a non-empty `code` field
- Steps should be concrete — no placeholders like `"code": "# TODO"` or `"code": "..."`
- `find`/`insert_after` anchors should look specific enough to locate the right spot
- Steps that only say "read X" or "test X" without producing code are invalid

**4. No duplicate actions**
- Two action files should not both modify the same file for the same purpose
- Merge candidates if they overlap heavily

**5. Ordering issues**
- If a JS action references a DOM element that a later HTML action creates, that is an issue
- Data model changes should come before code that uses the new fields

**6. Forbidden action types**
- Action files titled "Review", "Examine", "Analyze", "Test", "Verify", "Ensure" produce no code and must be removed

## VERDICT RULES

Submit **PASS** if:
- All spec requirements are addressed
- All file references are valid
- Every action file has concrete implementation steps with real code
- No critical structural issues

Submit **FAIL** if:
- One or more spec requirements are completely missed
- Any `files_to_modify` path does not exist in the project
- Any action file has empty `implementation_steps` or empty `code` fields
- There are duplicate actions for the same file+purpose

For **minor** issues (non-empty code but weak anchors, ordering could be better),
you may still PASS with the issue noted in `summary`.

## CALLING THE VERDICT TOOL

Call `submit_critic_verdict` exactly once with:
- `verdict`: "PASS" or "FAIL"
- `issues`: list of `{severity, file, description}` objects (empty if PASS)
  - `severity`: "critical" for issues that block coding, "minor" for warnings
  - `file`: the action filename (e.g. "T002.json") or "" for global issues
  - `description`: specific, actionable description of the problem
- `summary`: one sentence overall assessment

## EXAMPLES

PASS example:
```json
{
  "verdict": "PASS",
  "issues": [],
  "summary": "All 3 action files cover the spec requirements with concrete implementation steps."
}
```

FAIL example:
```json
{
  "verdict": "FAIL",
  "issues": [
    {
      "severity": "critical",
      "file": "T002.json",
      "description": "files_to_modify contains 'web/js/dashboard.js' which does not exist in the project files list."
    },
    {
      "severity": "critical",
      "file": "T003.json",
      "description": "implementation_steps is empty — no code to implement."
    }
  ],
  "summary": "2 action files have critical issues that would block the coding phase."
}
```
