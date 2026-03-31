"""
Persistent semantic index of all project files.

Stored at:  <project_root>/.tasks/project_index.json

Schema per entry:
{
  "src/api.py": {
    "description": "Defines REST API endpoints for user management.",
    "symbols":    ["UserRouter", "get_user", "create_user"],
    "imports":    ["fastapi", "models", "services.auth"],
    "used_by":    ["main.py", "tests/test_api.py"],
    "test_files": ["tests/test_api.py"],
    "type":       "code",        # code | image | config | doc | other
    "lang":       "python",
    "size_bytes": 2840,
    "mtime":      1711800000.0,
    "last_indexed": "2025-03-31T12:00:00"
  }
}
"""
from __future__ import annotations

import ast
import base64
import fnmatch
import json
import os
import re
import time
from typing import Callable, Optional

INDEX_FILENAME = "project_index.json"
TASKS_DIR      = ".tasks"

# ── Language detection ────────────────────────────────────────────
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".cs": "csharp",
    ".go": "go", ".rb": "ruby", ".php": "php",
    ".c": "c", ".cpp": "cpp", ".h": "c",
    ".rs": "rust", ".swift": "swift",
    ".sh": "shell", ".bash": "shell",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".xml": "xml", ".toml": "toml", ".ini": "ini",
    ".md": "markdown", ".rst": "rst", ".txt": "text",
    ".sql": "sql",
}
_IMAGE_EXTS   = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"}
_CONFIG_EXTS  = {".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".cfg", ".conf"}
_DOC_EXTS     = {".md", ".rst", ".txt"}
_SKIP_DIRS    = {"__pycache__", "node_modules", ".git", "dist", "build",
                 ".venv", "venv", ".env", "env", "site-packages"}
_SKIP_EXTS    = {".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe",
                 ".lock", ".log", ".DS_Store"}

# Rough estimate: 4 chars ≈ 1 token
_CHARS_PER_TOKEN = 4
_BATCH_TOKEN_LIMIT = 8_000

# Vision model name hints
_VISION_HINTS = {"llava", "bakllava", "moondream", "cogvlm", "minicpm-v"}


# ─────────────────────────────────────────────────────────────────
class ProjectIndex:

    def __init__(self, project_path: str):
        self.project_path = os.path.realpath(project_path)
        self.index_path   = os.path.join(self.project_path, TASKS_DIR, INDEX_FILENAME)
        self.data: dict[str, dict] = {}

    # ── Persistence ───────────────────────────────────────────────

    def load(self) -> None:
        if not os.path.isfile(self.index_path):
            self.data = {}
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception as e:
            print(f"[ProjectIndex] load failed: {e}", flush=True)
            self.data = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ProjectIndex] save failed: {e}", flush=True)

    # ── Public API ────────────────────────────────────────────────

    def scan_and_update(
        self,
        ollama,             # OllamaClient
        model: str,
        log_fn: Callable[[str], None],
        force: bool = False,
    ) -> None:
        """
        Walk project, find new/changed files, describe them via Ollama in batches.
        Existing entries whose mtime hasn't changed are left untouched.
        """
        self.load()
        gitignore_patterns = _parse_gitignore(self.project_path)

        all_files = _walk_project(self.project_path, gitignore_patterns)
        log_fn(f"  [Index] Found {len(all_files)} project files")

        # Determine which need (re)indexing
        to_index: list[str] = []
        for rel in all_files:
            abs_path = os.path.join(self.project_path, rel)
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                continue
            entry = self.data.get(rel)
            if force or entry is None or entry.get("mtime", 0) != mtime:
                to_index.append(rel)

        if not to_index:
            log_fn("  [Index] All files up to date")
            return

        log_fn(f"  [Index] Indexing {len(to_index)} new/changed files…")

        # Split into batches by token count
        batches = _make_batches(self.project_path, to_index)
        log_fn(f"  [Index] {len(batches)} batch(es) to send to Ollama")

        for i, batch in enumerate(batches, 1):
            log_fn(f"  [Index] Batch {i}/{len(batches)} — {len(batch)} files…")
            self._describe_batch(batch, ollama, model, log_fn)

        # Rebuild used_by for only the changed files
        self._rebuild_used_by(changed=to_index)
        self.save()
        log_fn(f"  [Index] Saved → {os.path.relpath(self.index_path, self.project_path)}")

    def update_files(
        self,
        changed_files: list[str],
        project_path: str,
        ollama,
        model: str,
        log_fn: Callable[[str], None],
    ) -> None:
        """
        Post-Coding: re-describe only files that were created/modified.
        changed_files: project-relative paths.
        """
        self.load()
        existing = [r for r in changed_files
                    if os.path.isfile(os.path.join(project_path, r))]
        if not existing:
            return
        log_fn(f"  [Index] Updating {len(existing)} changed file(s)…")
        batches = _make_batches(project_path, existing)
        for batch in batches:
            self._describe_batch(batch, ollama, model, log_fn)
        self._rebuild_used_by(changed=existing)
        self.save()
        log_fn("  [Index] Updated")

    def get_relevant_files(
        self, task_description: str, top_n: int = 25
    ) -> list[tuple[str, float]]:
        """
        Static keyword-based relevance scoring.
        Returns [(rel_path, score), ...] sorted by score desc.
        """
        keywords = _extract_keywords(task_description)
        if not keywords:
            return list(self.data.keys())[:top_n]

        scores: dict[str, float] = {}
        for rel, entry in self.data.items():
            text = " ".join([
                entry.get("description", ""),
                " ".join(entry.get("symbols", [])),
                " ".join(entry.get("imports", [])),
                rel,
            ]).lower()
            score = sum(1.0 for kw in keywords if kw in text)
            if score > 0:
                scores[rel] = score

        # Boost direct dependencies of high-score files
        high_score = {r for r, s in scores.items() if s >= 2}
        for rel in list(high_score):
            for dep in self.data.get(rel, {}).get("imports", []):
                # Find project file matching this import
                match = _import_to_file(dep, self.data)
                if match and match not in scores:
                    scores[match] = 0.5  # dependency bonus

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_n]

    def format_for_prompt(self, files: Optional[list[str]] = None) -> str:
        """
        Compact text summary of index entries for use in Ollama prompts.
        One line per file: path | description | symbols | imports
        """
        entries = files or list(self.data.keys())
        lines = []
        for rel in entries:
            e = self.data.get(rel)
            if not e:
                lines.append(rel)
                continue
            sym  = ", ".join(e.get("symbols", [])[:8])
            imp  = ", ".join(e.get("imports", [])[:6])
            desc = e.get("description", "")
            line = f"{rel} | {desc}"
            if sym:
                line += f" | symbols: {sym}"
            if imp:
                line += f" | imports: {imp}"
            lines.append(line)
        return "\n".join(lines)

    def validate(self, log_fn: Callable[[str], None]) -> tuple[bool, list[str]]:
        """
        Check that all indexed files actually exist on disk.
        Returns (all_ok, list_of_issues).
        """
        issues: list[str] = []
        missing = []
        for rel in self.data:
            if not os.path.isfile(os.path.join(self.project_path, rel)):
                missing.append(rel)

        if missing:
            for m in missing:
                issues.append(f"Indexed file no longer exists: {m}")
                log_fn(f"  [Index] WARNING: missing file: {m}")
            # Remove stale entries
            for m in missing:
                del self.data[m]
            self.save()

        return (len(issues) == 0), issues

    # ── Internal: batch describe ──────────────────────────────────

    def _describe_batch(
        self,
        batch: list[str],
        ollama,
        model: str,
        log_fn: Callable[[str], None],
    ) -> None:
        """
        Send a batch of files to Ollama and update self.data with descriptions.
        Images are handled separately (vision model check).
        """
        code_batch  = [r for r in batch if _file_type(r) != "image"]
        image_batch = [r for r in batch if _file_type(r) == "image"]

        if code_batch:
            self._describe_code_batch(code_batch, ollama, model, log_fn)

        for rel in image_batch:
            self._describe_image(rel, ollama, model, log_fn)

    def _describe_code_batch(
        self,
        batch: list[str],
        ollama,
        model: str,
        log_fn: Callable[[str], None],
    ) -> None:
        # Build file sections
        sections: list[str] = []
        for rel in batch:
            abs_path = os.path.join(self.project_path, rel)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                content = "(unreadable)"
            # Truncate large files — first 300 lines is usually enough for a description
            lines = content.splitlines()
            if len(lines) > 300:
                content = "\n".join(lines[:300]) + f"\n…(truncated, {len(lines)} lines total)"
            sections.append(f"=== {rel} ===\n{content}")

        batch_text = "\n\n".join(sections)

        prompt = (
            "Analyze the following source files and return ONLY a JSON object.\n"
            "For each file path key, return an object with:\n"
            '  "description": one sentence (max 20 words) describing what the file does\n\n'
            "Return ONLY valid JSON. No markdown, no explanation, no backticks.\n"
            "Example: {\"src/api.py\": {\"description\": \"Defines REST endpoints.\"}}\n\n"
            + batch_text
        )

        try:
            response = ollama.complete(model=model, prompt=prompt, max_tokens=1500)
            parsed = _parse_json_response(response)
        except Exception as e:
            log_fn(f"  [Index] Ollama batch failed: {e}", )
            parsed = {}

        now_ts  = time.strftime("%Y-%m-%dT%H:%M:%S")
        for rel in batch:
            abs_path = os.path.join(self.project_path, rel)
            try:
                mtime      = os.path.getmtime(abs_path)
                size_bytes = os.path.getsize(abs_path)
            except OSError:
                mtime, size_bytes = 0.0, 0

            static     = _extract_static_info(abs_path)
            ollama_res = parsed.get(rel, {})
            description = ollama_res.get("description", "")

            existing = self.data.get(rel, {})
            self.data[rel] = {
                "description":  description or existing.get("description", ""),
                "symbols":      static["symbols"],
                "imports":      static["imports"],
                "used_by":      existing.get("used_by", []),   # rebuilt separately
                "test_files":   _find_test_files(rel, self.data),
                "type":         _file_type(rel),
                "lang":         _file_lang(rel),
                "size_bytes":   size_bytes,
                "mtime":        mtime,
                "last_indexed": now_ts,
            }

    def _describe_image(
        self,
        rel: str,
        ollama,
        model: str,
        log_fn: Callable[[str], None],
    ) -> None:
        abs_path = os.path.join(self.project_path, rel)
        try:
            mtime      = os.path.getmtime(abs_path)
            size_bytes = os.path.getsize(abs_path)
        except OSError:
            mtime, size_bytes = 0.0, 0

        now_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        description = ""

        # Check if model supports vision
        is_vision = any(hint in model.lower() for hint in _VISION_HINTS)
        if is_vision and abs_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            try:
                with open(abs_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                ext = os.path.splitext(abs_path)[1].lstrip(".").lower()
                mime = {"jpg": "jpeg"}.get(ext, ext)
                prompt = "Describe this image in one sentence (max 20 words). Only the description, no other text."
                description = ollama.complete_vision(
                    model=model, prompt=prompt,
                    image_b64=img_b64, mime_type=f"image/{mime}",
                    max_tokens=100,
                )
            except Exception as e:
                log_fn(f"  [Index] Vision describe failed for {rel}: {e}")

        if not description:
            name = os.path.splitext(os.path.basename(rel))[0].replace("_", " ").replace("-", " ")
            description = f"Image file: {name}."

        self.data[rel] = {
            "description":  description,
            "symbols":      [],
            "imports":      [],
            "used_by":      [],
            "test_files":   [],
            "type":         "image",
            "lang":         "",
            "size_bytes":   size_bytes,
            "mtime":        mtime,
            "last_indexed": now_ts,
        }

    # ── Internal: used_by graph ───────────────────────────────────

    def _rebuild_used_by(self, changed: Optional[list[str]] = None) -> None:
        """
        Rebuild used_by reverse dependency edges.
        If `changed` is given: only recalculate for files whose imports changed.
        """
        # Build forward map: rel → set of project-relative paths it imports
        # (resolved from import names)
        all_files = set(self.data.keys())

        # Clear used_by for changed files and files that depend on them
        files_to_recalc = set(changed) if changed else all_files
        for rel in files_to_recalc:
            if rel in self.data:
                self.data[rel]["used_by"] = []

        # For each file that might reference any of `changed`:
        # rebuild its contribution to used_by
        for rel, entry in self.data.items():
            for imp in entry.get("imports", []):
                target = _import_to_file(imp, self.data)
                if target and target in files_to_recalc:
                    current = self.data[target].get("used_by", [])
                    if rel not in current:
                        current.append(rel)
                    self.data[target]["used_by"] = current


# ─────────────────────────────────────────────────────────────────
# Static analysis helpers
# ─────────────────────────────────────────────────────────────────

def _extract_static_info(abs_path: str) -> dict:
    """Extract symbols and imports without Ollama."""
    ext = os.path.splitext(abs_path)[1].lower()
    symbols: list[str] = []
    imports: list[str] = []

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return {"symbols": symbols, "imports": imports}

    if ext == ".py":
        symbols, imports = _extract_python(content)
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        symbols, imports = _extract_js(content)
    elif ext in (".java", ".kt"):
        symbols, imports = _extract_java(content)
    elif ext in (".go",):
        symbols, imports = _extract_go(content)
    elif ext in (".json",):
        imports = []
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                symbols = list(data.keys())[:10]
        except Exception:
            pass
    elif ext in (".html", ".htm"):
        symbols, imports = _extract_html(content)
    elif ext in (".css", ".scss"):
        # CSS class/id selectors
        symbols = re.findall(r'[.#]([a-zA-Z][\w-]*)\s*\{', content)[:20]

    return {"symbols": list(dict.fromkeys(symbols))[:25],
            "imports": list(dict.fromkeys(imports))[:25]}


def _extract_python(content: str) -> tuple[list[str], list[str]]:
    symbols: list[str] = []
    imports: list[str] = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    symbols.append(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    # Keep relative import resolution: store full dotted path
                    imports.append(node.module)
    except SyntaxError:
        pass
    return symbols, imports


def _extract_js(content: str) -> tuple[list[str], list[str]]:
    symbols = re.findall(
        r'(?:export\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)',
        content
    )
    imports = re.findall(
        r"""(?:import|require)\s*(?:[^'"]*from\s*)?['"]([\w@./\\-]+)['"]""",
        content
    )
    return symbols[:25], imports[:25]


def _extract_java(content: str) -> tuple[list[str], list[str]]:
    symbols = re.findall(
        r'(?:public|private|protected|static)?\s*(?:class|interface|enum|record)\s+(\w+)',
        content
    )
    imports = re.findall(r'import\s+([\w.]+);', content)
    return symbols[:25], [i.rsplit(".", 1)[-1] for i in imports[:25]]


def _extract_go(content: str) -> tuple[list[str], list[str]]:
    symbols = re.findall(r'^func\s+(\w+)', content, re.MULTILINE)
    imports = re.findall(r'"([\w./]+)"', content)
    return symbols[:25], imports[:25]


def _extract_html(content: str) -> tuple[list[str], list[str]]:
    # script src, link href
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', content, re.I)
    links   = re.findall(r'<link[^>]+href=["\']([^"\']+)["\']',   content, re.I)
    ids     = re.findall(r'id=["\']([^"\']+)["\']', content, re.I)
    return ids[:20], (scripts + links)[:20]


# ─────────────────────────────────────────────────────────────────
# File classification helpers
# ─────────────────────────────────────────────────────────────────

def _file_type(rel: str) -> str:
    ext = os.path.splitext(rel)[1].lower()
    if ext in _IMAGE_EXTS:   return "image"
    if ext in _CONFIG_EXTS:  return "config"
    if ext in _DOC_EXTS:     return "doc"
    if ext in _EXT_TO_LANG:  return "code"
    return "other"


def _file_lang(rel: str) -> str:
    return _EXT_TO_LANG.get(os.path.splitext(rel)[1].lower(), "")


# ─────────────────────────────────────────────────────────────────
# .gitignore handling
# ─────────────────────────────────────────────────────────────────

def _parse_gitignore(project_path: str) -> list[str]:
    patterns: list[str] = []
    gi_path = os.path.join(project_path, ".gitignore")
    if not os.path.isfile(gi_path):
        return patterns
    try:
        with open(gi_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except Exception:
        pass
    return patterns


def _is_gitignored(rel: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any .gitignore pattern."""
    parts = rel.replace("\\", "/").split("/")
    for pattern in patterns:
        pattern = pattern.strip("/")
        # Match against each path component and the full path
        if fnmatch.fnmatch(rel, pattern):
            return True
        if fnmatch.fnmatch(rel, pattern + "/*"):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        # Check prefix match for directory patterns
        if not pattern.startswith("*") and rel.startswith(pattern):
            return True
    return False


# ─────────────────────────────────────────────────────────────────
# File walker
# ─────────────────────────────────────────────────────────────────

def _walk_project(project_path: str, gitignore_patterns: list[str]) -> list[str]:
    """Return all indexable project-relative file paths."""
    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_path):
        # Prune directories in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS
            and not _is_gitignored(
                os.path.relpath(os.path.join(dirpath, d), project_path).replace("\\", "/"),
                gitignore_patterns,
            )
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SKIP_EXTS:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel = os.path.relpath(abs_path, project_path).replace("\\", "/")
            if _is_gitignored(rel, gitignore_patterns):
                continue
            result.append(rel)
    return sorted(result)


# ─────────────────────────────────────────────────────────────────
# Batching
# ─────────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _make_batches(project_path: str, files: list[str]) -> list[list[str]]:
    """Split file list into batches where each batch fits within token budget."""
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_tokens = 0

    for rel in files:
        abs_path = os.path.join(project_path, rel)
        ext = os.path.splitext(rel)[1].lower()

        # Images are batched 1 per call (vision needs separate handling)
        if ext in _IMAGE_EXTS:
            batches.append([rel])
            continue

        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0

        # Estimate tokens for this file (truncated at 300 lines ~ 300*80 chars)
        effective_chars = min(size, 300 * 80)
        file_tokens = _estimate_tokens(rel + ": " + " " * effective_chars)

        if current_batch and (current_tokens + file_tokens) > _BATCH_TOKEN_LIMIT:
            batches.append(current_batch)
            current_batch = [rel]
            current_tokens = file_tokens
        else:
            current_batch.append(rel)
            current_tokens += file_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


# ─────────────────────────────────────────────────────────────────
# Relevance scoring
# ─────────────────────────────────────────────────────────────────

_STOP_WORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "is",
    "it", "be", "as", "by", "with", "this", "that", "from", "are", "was",
    "add", "create", "update", "change", "make", "new", "need", "should",
    "must", "will", "also", "all", "any", "into", "not",
}


def _extract_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z_]\w*", text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


# ─────────────────────────────────────────────────────────────────
# Import → file resolution
# ─────────────────────────────────────────────────────────────────

def _import_to_file(import_name: str, index_data: dict) -> Optional[str]:
    """
    Try to resolve an import name to a project-relative file path.
    Handles Python dotted paths (e.g. "core.state" → "core/state.py").
    """
    # Try direct dotted-to-slash mapping
    candidates = [
        import_name.replace(".", "/") + ".py",
        import_name.replace(".", "/") + ".js",
        import_name.replace(".", "/") + ".ts",
        import_name.replace(".", "/") + "/index.js",
        import_name.replace(".", "/") + "/index.ts",
    ]
    for c in candidates:
        if c in index_data:
            return c

    # Try basename match (last segment of dotted import)
    basename = import_name.split(".")[-1]
    for rel in index_data:
        fname = os.path.splitext(os.path.basename(rel))[0]
        if fname == basename:
            return rel

    return None


# ─────────────────────────────────────────────────────────────────
# Test file detection
# ─────────────────────────────────────────────────────────────────

def _find_test_files(rel: str, index_data: dict) -> list[str]:
    """Find test files that likely cover this file."""
    base = os.path.splitext(os.path.basename(rel))[0]
    patterns = [
        f"test_{base}",
        f"{base}_test",
        f"test_{base.replace('-', '_')}",
    ]
    result = []
    for candidate in index_data:
        cname = os.path.splitext(os.path.basename(candidate))[0].lower()
        if any(cname == p for p in patterns):
            result.append(candidate)
    return result


# ─────────────────────────────────────────────────────────────────
# JSON response parser (tolerant)
# ─────────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> dict:
    """Try to extract a JSON object from Ollama's response, tolerantly."""
    if not text:
        return {}
    # Strip markdown fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    # Find outermost { ... }
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
