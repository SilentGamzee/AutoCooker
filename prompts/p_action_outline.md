# Action Outline (Step 2a)

Break the task into discrete subtasks — one per coherent unit of work
(typically one project file per subtask). This is a LIGHT step: no
code blocks, no search/replace, just a subtask list.

## YOUR JOB

1. Read the SPECIFICATION in the message.
2. Use the KEY SOURCE FILES and the project file list to decide which
   files need changes. If you need more files, **call
   `read_files_batch` with ALL paths you need in a single call** —
   never loop `read_file`.
3. Write `subtasks_outline.json` via `write_file`.
4. Call `confirm_phase_done`.

## Schema — exact shape required

```json
{
  "subtasks": [
    {
      "id": "T-001",
      "title": "Short imperative title",
      "files_to_modify": ["rel/path/to/file.py"],
      "files_to_create": [],
      "brief": "1-2 sentences: what this subtask does and why."
    }
  ]
}
```

## Rules

- **One file per subtask** is the default. Group files only when they
  MUST change atomically (e.g. a new Python endpoint and the JS call
  site — these are still usually TWO subtasks, one per file, ordered
  backend-first).
- **Cover every spec requirement and acceptance criterion.** If you
  miss one, Step 2c will flag it and we'll re-enter Step 2b for it —
  so catching it here saves a round-trip.
- **No "review/analyze/test" subtasks.** Every subtask must produce
  real code changes in project files.
- **Use sequential ids starting from T-001**: T-001, T-002, T-003 …
  Always start from T-001 for every new outline, regardless of any
  previous action files on disk.
- **Ordering matters**: data/state before backend, backend before
  frontend, HTML before JS that binds to it, CSS last.
- **Do NOT write any other file here** — only `subtasks_outline.json`.

## DO NOT
- Emit `implementation_steps`, `blocks`, `search`, `replace`,
  `create`, or any code. That's Step 2b's job.
- Use `read_file` in a loop. Always `read_files_batch(paths=[…])`.
- Include subtasks whose only output is a log, a summary, or a README.
