"""Non-Ollama validators for output files."""
from __future__ import annotations
import json
import os
import re
from typing import Tuple


def _load_json(path: str) -> Tuple[bool, dict | list | None, str]:
    if not os.path.isfile(path):
        return False, None, f"File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return True, data, ""
    except json.JSONDecodeError as e:
        return False, None, f"JSON parse error: {e}"


def validate_task_info(path: str) -> Tuple[bool, str]:
    """Validate task_NNN.json has required fields."""
    ok, data, err = _load_json(path)
    if not ok:
        return False, err
    required = ["name", "description", "models", "git_branch", "project_path", "task_dir"]
    missing = [k for k in required if k not in data]
    if missing:
        return False, f"Missing fields: {missing}"
    if not isinstance(data.get("models"), (list, dict)):
        return False, "Field 'models' must be a list or dict"
    return True, "OK"


def validate_assessment(path: str) -> Tuple[bool, str]:
    """Validate assessment.json."""
    ok, data, err = _load_json(path)
    if not ok:
        return False, err
    required = ["hours", "complexity", "min_tasks", "files_analyzed"]
    missing = [k for k in required if k not in data]
    if missing:
        return False, f"Missing fields: {missing}"
    if data.get("complexity") not in ("Simple", "Standard", "Complex"):
        return False, "Field 'complexity' must be Simple | Standard | Complex"
    try:
        min_tasks = int(data["min_tasks"])
        if min_tasks < 1:
            raise ValueError
    except (TypeError, ValueError):
        return False, "Field 'min_tasks' must be a positive integer"
    return True, "OK"


def validate_subtasks(path: str, expected_min: int = 1) -> Tuple[bool, str]:
    """Validate subtasks.json."""
    ok, data, err = _load_json(path)
    if not ok:
        return False, err
    if not isinstance(data, list):
        return False, "subtasks.json must be a JSON array"
    if len(data) < expected_min:
        return False, f"Expected at least {expected_min} tasks, got {len(data)}"
    required_keys = [
        "id", "title", "description",
        "completion_with_ollama", "completion_without_ollama",
    ]
    for i, task in enumerate(data):
        missing = [k for k in required_keys if k not in task]
        if missing:
            return False, f"Task[{i}] missing fields: {missing}"
        if not task.get("title", "").strip():
            return False, f"Task[{i}] has empty title"
    return True, "OK"


def validate_json_file(path: str) -> Tuple[bool, str]:
    """Generic JSON validation."""
    ok, _, err = _load_json(path)
    return ok, err or "OK"


def validate_file_exists(path: str) -> Tuple[bool, str]:
    if os.path.isfile(path):
        return True, "OK"
    return False, f"File does not exist: {path}"


def validate_directory_exists(path: str) -> Tuple[bool, str]:
    if os.path.isdir(path):
        return True, "OK"
    return False, f"Directory does not exist: {path}"


def validate_xml_like(content: str) -> Tuple[bool, str]:
    """Very basic XML / HTML structural check."""
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(f"<root>{content}</root>")
        return True, "OK"
    except ET.ParseError as e:
        return False, f"XML parse error: {e}"


def validate_readme(path: str) -> Tuple[bool, str]:
    """Check README.md exists and has minimal content."""
    ok, msg = validate_file_exists(path)
    if not ok:
        return ok, msg
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if len(content.strip()) < 100:
        return False, "README.md seems too short (< 100 chars)"
    # Check for at least one heading
    if not re.search(r"^#+\s", content, re.MULTILINE):
        return False, "README.md has no Markdown headings"
    return True, "OK"
