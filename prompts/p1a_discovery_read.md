# Discovery — READ PHASE (Step 1.1a)

You are in the READ PHASE. Your only job is to read files. You have 5 rounds. `write_file` is NOT available.

## RULES
- Batch multiple reads per round — never read one file per round
- NEVER re-read a file already in `History of tool calls:` (content is in `Read files from last call:`)
- Do NOT read `.tasks/` directory
- Do NOT explain plans — just read files

## ROUND GUIDE
```
Round 1: list_directory(root) + list_directory(core/) + read_file(main.py) + read_file(requirements.txt)
Round 2: read_file(core/state.py) + read_file(core/phases/planning.py) + read_file(web/index.html)
Round 3: read files relevant to this specific task (similar features, data models)
Rounds 4-5: imports of task-relevant files, wiring layers (__init__.py, router.py)
```

## CROSS-FILE RULE
For every file mentally marked "to modify":
1. What does it import? → Read those too
2. What imports IT? → Those may also need changes
3. Frontend/backend counterpart? → Read both

After round 5 you transition automatically to Write Phase — make sure you've read everything needed.
