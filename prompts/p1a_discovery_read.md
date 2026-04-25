# Discovery — READ PHASE (Step 1.1a)

You are in the READ PHASE. Your only job is to read files. You have 5 rounds. `write_file` is NOT available.

## RULES
- Use `read_files_batch` to read multiple files in ONE call — never call `read_file` one file at a time
- NEVER re-read a file already in `History of tool calls:` (content is in `Read files from last call:`)
- Do NOT read `.tasks/` directory
- Do NOT explain plans — just read files

## ROUND GUIDE
```
Round 1: list_directory(root) + read_files_batch(["main.py", "requirements.txt", "core/state.py"])
Round 2: read_files_batch(["core/phases/planning.py", "web/index.html", "web/js/app.js"])
Round 3: read_files_batch([<files relevant to this specific task>])
Rounds 4-5: read_files_batch([<imports of task-relevant files, wiring layers>])
```

## CROSS-FILE RULE
For every file mentally marked "to modify":
1. What does it import? → Read those too
2. What imports IT? → Those may also need changes
3. Frontend/backend counterpart? → Read both

After round 5 you transition automatically to Write Phase — make sure you've read everything needed.
