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
      "brief": "1-2 sentences: what this subtask does and why.",
      "region": {
        "file": "rel/path/to/file.py",
        "anchor_symbol": "ClassName.method or #element-id or .css-selector",
        "start_line": 386,
        "end_line": 457
      }
    }
  ]
}
```

`region` is OPTIONAL for files modified by exactly one subtask, REQUIRED for shared files (see below).

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

## Already-implemented short-circuit

Before generating any subtasks, scan the project files (especially any
"POSSIBLY ALREADY IMPLEMENTED" hints in the user message). If existing
code already satisfies every acceptance criterion of the spec, do NOT
invent duplicate subtasks. Instead emit:

```json
{"subtasks": [], "already_implemented": true,
 "evidence": "main.py:_process_queue (L1112-1145) already moves Queue→InProgress when a slot is free — covers AC-1/AC-2/AC-3"}
```

`evidence` MUST cite file:symbol:line-range and explain how it covers the
acceptance criteria. The pipeline routes such tasks straight to review.

## Cross-subtask contracts — `provides` / `consumes`

When two subtasks talk to each other (one defines a field, function, or
DOM id; another reads it), declare the contract so the writer phase
cannot drift on names.

- `provides` — list of identifiers this subtask defines or exports.
  Examples: `KanbanTask.attachments`, `upload_attachment`, `esc`,
  `populateAttachmentList`, `#new-attachments`, `.att-empty`.
- `consumes` — list of identifiers this subtask references that another
  subtask must provide first. Same naming convention as `provides`.

Rules:

- For every item in `consumes`, some EARLIER subtask MUST list the same
  string in `provides` (or it must already exist in the project
  `outline`). The validator rejects orphan `consumes`.
- Order subtasks so producers come before consumers (data → backend →
  HTML → JS → CSS already enforces this for files; `provides` /
  `consumes` enforces it at the symbol level).
- Use full names: include class for methods (`KanbanTask.to_dict`),
  `#id` for DOM ids, `.cls` for CSS classes. Avoid generic words
  (`task`, `data`).
- Naming rules:
  - **Bare** function/class names (`_process_queue`, `KanbanTask`).
  - **Class members**: `Class.method` / `Class.field` (e.g. `KanbanTask.to_dict`, `AppState.kanban_tasks`).
  - **DOM/CSS**: `#id`, `.class`.
  - Do **NOT** prefix with module path (`main.foo` is wrong → use `foo`).
  - For singleton/global state, use the dataclass form (`AppState.kanban_tasks`), not the instance-attr form (`STATE.kanban_tasks`).

Example:

```json
{"id": "T-001", "files_to_modify": ["core/state.py"],
 "provides": ["KanbanTask.attachments", "KanbanTask.attachment_data_uri"]}
{"id": "T-005", "files_to_modify": ["web/js/app.js"],
 "consumes": ["KanbanTask.attachments", "KanbanTask.attachment_data_uri", "#new-attachments"],
 "provides": ["populateAttachmentList", "esc"]}
```

## Shared-file regions — MANDATORY when ≥2 subtasks share a file

If a single file appears in `files_to_modify` of two or more subtasks, EACH such subtask MUST include a `region` object:

- `anchor_symbol` — pick from the `outline` of that file in the project index (formats like `KanbanTask`, `KanbanTask.to_dict`, `#new-task-modal`, `.attachment-list`). Use a symbol within the area you intend to patch.
- `start_line` / `end_line` — absolute line numbers covering the area. Use the line range shown in the project index outline (`@ L386-457`) and extend by ≤10 lines if needed.

Hard rules for regions on shared files:

- Regions of subtasks targeting the same file MUST NOT overlap. Leave at least 1 line of gap between them.
- Order subtasks by ascending `start_line` for the same file.
- If you cannot identify two non-overlapping regions, MERGE the work into a single subtask instead.
- **If you don't know the line numbers**, call `read_files_batch(paths=["<shared_file>"])` BEFORE writing the outline. The truncated view + skeleton in the tool result lists every symbol with its absolute line. Pick non-overlapping `start_line`/`end_line` from there. NEVER guess — the validator will reject missing or overlapping regions and force a retry.
- The validator is mechanical: missing `region.start_line` or `region.end_line` on any shared-file subtask blocks the entire outline. Every shared-file subtask must satisfy the schema before submission.

Example — two subtasks touching `web/index.html`:

```json
{"id": "T-002", "files_to_modify": ["web/index.html"],
 "region": {"file": "web/index.html", "anchor_symbol": "#new-task-modal",
            "start_line": 120, "end_line": 180}}
{"id": "T-004", "files_to_modify": ["web/index.html"],
 "region": {"file": "web/index.html", "anchor_symbol": "#task-detail-modal",
            "start_line": 240, "end_line": 310}}
```

## DO NOT
- Emit `implementation_steps`, `blocks`, `search`, `replace`,
  `create`, or any code. That's Step 2b's job.
- Use `read_file` in a loop. Always `read_files_batch(paths=[…])`.
- Include subtasks whose only output is a log, a summary, or a README.
