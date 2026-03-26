# Ollama Project Planner

Kanban-based AI coding assistant powered by local Ollama models.

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) running locally (`http://localhost:11434`)
- At least one model pulled: `ollama pull llama3.1`
- Chrome or Chromium (used by Eel for the GUI)

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

## Kanban Columns

| Column | Description |
|--------|-------------|
| Planning | New tasks waiting to be started |
| Queue | Tasks queued for parallel execution |
| In Progress | Pipeline currently running |
| AI Review | Awaiting AI quality review |
| Human Review | Needs human attention (errors found) |
| Done | Completed successfully |

## Task Modal Tabs

- **Overview** — metadata, models, run/abort controls
- **Subtasks** — expandable list of subtasks created during Planning phase
- **Logs** — structured log viewer grouped by Planning / Coding / QA phase, color-coded by type, auto-scrolled to latest
- **Files** — hierarchical file explorer of the project directory

## Pipeline Phases

1. **Planning** — Creates task info JSON, assesses complexity, generates subtasks, validates output
2. **Coding** — Executes each subtask, generates README, runs tests
3. **QA** — Verifies completion, runs tests, validates JSON/XML, checks text quality

## Log Entry Colors

| Color | Type |
|-------|------|
| Blue | `write_file` / `modify_file` tool calls |
| Yellow | `list_directory` searches |
| Cyan | `read_file` calls |
| Green | Confirmations, success messages |
| Red | Errors |
| Orange/Yellow | Warnings |
| Purple (bright) | Ollama responses |
| White | Phase headers |
