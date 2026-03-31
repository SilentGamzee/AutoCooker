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


# ═════════════════════════════════════════════════════════════════
# CrossDepsAnalyzer — universal static cross-file dependency graph
# ═════════════════════════════════════════════════════════════════

class CrossDepsAnalyzer:
    """
    Language-agnostic static analyzer for cross-file dependencies.

    Works entirely without an LLM. Approach:
      1. Scan every file for references to other files (imports, requires,
         includes, url() calls, src= / href= attributes, etc.)
      2. Extract semantic cross-references that imply indirect dependencies:
         CSS classes, DOM IDs, API endpoints, event names, env vars.
      3. Resolve raw reference strings to actual project files where possible.
      4. Build a forward graph (file → what it needs) and reverse graph
         (file → who needs it).

    The result is passed to the Discovery agent so it can populate
    context.json correctly without guessing.
    """

    # ── Import / file-reference patterns per extension group ─────
    # Each entry: (tuple-of-extensions, list-of-regex-patterns)
    # Each pattern must have exactly ONE capture group: the raw reference value.
    _IMPORT_RULES: list[tuple[tuple[str, ...], list[str]]] = [
        # Python
        ((".py",), [
            r'^\s*import\s+([\w.]+)',
            r'^\s*from\s+([\w.]+)\s+import',
        ]),
        # JavaScript / TypeScript (ESM + CJS)
        ((".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"), [
            r'(?:import|export)[^"\']*["\']([^"\']+)["\']',
            r'require\s*\(\s*["\']([^"\']+)["\']',
            r'import\s*\(\s*["\']([^"\']+)["\']',          # dynamic import()
        ]),
        # CSS / SCSS / LESS
        ((".css", ".scss", ".sass", ".less"), [
            r'@import\s+["\']([^"\']+)["\']',
            r'@use\s+["\']([^"\']+)["\']',
            r'@forward\s+["\']([^"\']+)["\']',
            r'url\(\s*["\']?([^"\')\s]+)["\']?\s*\)',
        ]),
        # HTML / Jinja / Twig / Django templates
        ((".html", ".htm", ".jinja", ".jinja2", ".j2", ".tpl", ".twig"), [
            r'<script[^>]+src=["\']([^"\']+)["\']',
            r'<link[^>]+href=["\']([^"\']+)["\']',
            r'<img[^>]+src=["\']([^"\']+)["\']',
            r'@include\s*["\']([^"\']+)["\']',             # Blade / Twig
            r'\{%\s*include\s+["\']([^"\']+)["\']',        # Jinja
            r'\{%\s*extends\s+["\']([^"\']+)["\']',        # Jinja
        ]),
        # Go
        ((".go",), [
            r'import\s+"([^"]+)"',
            r'"\s*([a-z][\w./\-]+)"',                       # multi-import block
        ]),
        # Rust
        ((".rs",), [
            r'\buse\s+([\w:]+)',
            r'\bmod\s+(\w+)',
            r'include!\s*\(\s*["\']([^"\']+)["\']',
        ]),
        # C / C++
        ((".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"), [
            r'#include\s*[<"]([\w./]+)[>"]',
        ]),
        # Java / Kotlin
        ((".java", ".kt", ".kts"), [
            r'import\s+([\w.]+)',
        ]),
        # Ruby
        ((".rb", ".rake"), [
            r'require\s+["\']([^"\']+)["\']',
            r'require_relative\s+["\']([^"\']+)["\']',
            r'load\s+["\']([^"\']+)["\']',
        ]),
        # PHP
        ((".php",), [
            r'(?:require|include)(?:_once)?\s*["\']([^"\']+)["\']',
            r'\buse\s+([\w\\]+)',
        ]),
        # Swift
        ((".swift",), [
            r'\bimport\s+(\w+)',
        ]),
        # Dart / Flutter
        ((".dart",), [
            r"import\s+['\"]([^'\"]+)['\"]",
            r"part\s+['\"]([^'\"]+)['\"]",
        ]),
        # Vue SFC
        ((".vue",), [
            r'<script[^>]*src=["\']([^"\']+)["\']',
            r'(?:import|from)\s+["\']([^"\']+)["\']',
            r'@import\s+["\']([^"\']+)["\']',
        ]),
        # YAML / GitHub Actions / Docker Compose
        ((".yml", ".yaml"), [
            r'uses:\s+([\w./\-@:]+)',                       # GitHub Actions
            r'image:\s+([\w./\-:]+)',                       # Docker Compose
        ]),
        # Shell scripts
        ((".sh", ".bash", ".zsh"), [
            r'\bsource\s+["\']?([^\s"\']+)["\']?',
            r'\.\s+["\']?([^\s"\']+)["\']?',
        ]),
        # Dockerfile
        ((".dockerfile",), [
            r'^FROM\s+([\w./\-:]+)',
            r'^COPY\s+([^\s]+)',
        ]),
        # Makefile / CMake
        (("makefile", "cmakelists.txt"), [
            r'include\s+([^\s]+)',
        ]),
    ]

    # ── Semantic cross-reference patterns ─────────────────────────
    # These extract values (class names, IDs, endpoints, etc.) that
    # create implicit dependencies between files even without imports.
    _SEMANTIC_RULES: dict[str, list[str]] = {
        # CSS class references in code/markup
        "css_classes": [
            r'classList\.(?:add|remove|toggle|contains|replace)\s*\(\s*["\']([^"\']+)["\']',
            r'className\s*[+]?=\s*["\']([^"\']+)["\']',
            r'class=["\']([^"\']+)["\']',
            r'\.addClass\s*\(\s*["\']([^"\']+)["\']',       # jQuery
            r'styled\.[a-zA-Z]+`',                           # styled-components marker
            r'css\(["\']([^"\']+)["\']',                     # emotion/css-in-js
        ],
        # DOM element IDs referenced across files
        "dom_ids": [
            r'getElementById\s*\(\s*["\']([^"\']+)["\']',
            r'querySelector\s*\(\s*["\']#([^"\']+)["\']',
            r'\bid=["\']([^"\']+)["\']',
        ],
        # API endpoint strings (frontend calls + backend definitions)
        "api_endpoints": [
            r'fetch\s*\(\s*["\']([^"\']+)["\']',
            r'axios\.(?:get|post|put|patch|delete|head)\s*\(\s*["\']([^"\']+)["\']',
            r'(?:\$http|http)\.(?:get|post|put|delete)\s*\(\s*["\']([^"\']+)["\']',
            r'@(?:app|router|blueprint)\.(?:route|get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
            r'@(?:Get|Post|Put|Delete|Patch)Mapping\s*\(\s*["\']([^"\']+)["\']',  # Spring
            r'Route::(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',  # Laravel
            r'path\s*\(\s*["\']([^"\']+)["\']',             # Django urls
        ],
        # Event names: pub/sub, EventEmitter, DOM events (custom)
        "event_names": [
            r'(?:emit|trigger|dispatch|publish)\s*\(\s*["\']([^"\']+)["\']',
            r'(?:on|addEventListener|once|subscribe)\s*\(\s*["\']([^"\']+)["\']',
            r'(?:off|removeEventListener|unsubscribe)\s*\(\s*["\']([^"\']+)["\']',
            r'NOTIFICATION_CENTER\.post\s*\(\s*name:\s*["\']([^"\']+)["\']',  # iOS
        ],
        # Environment / config variable names
        "env_vars": [
            r'os\.(?:getenv|environ\.get)\s*\(\s*["\']([^"\']+)["\']',
            r'process\.env\.([A-Z_][A-Z0-9_]*)',
            r'ENV\[["\']([^"\']+)["\']',
            r'System\.getenv\s*\(\s*["\']([^"\']+)["\']',  # Java
            r'Environment\.GetEnvironmentVariable\s*\(\s*["\']([^"\']+)["\']',  # C#
            r'std::env::var\s*\(\s*["\']([^"\']+)["\']',   # Rust
        ],
        # RPC / bridge calls (Eel, Electron ipc, Tauri, Capacitor, etc.)
        "rpc_calls": [
            r'eel\.(\w+)\s*\(',                             # Python Eel
            r'ipcRenderer\.(?:send|invoke|on)\s*\(\s*["\']([^"\']+)["\']',  # Electron
            r'ipcMain\.(?:handle|on)\s*\(\s*["\']([^"\']+)["\']',
            r'window\.__TAURI__\.[.\w]+\s*\(',              # Tauri
            r'Capacitor\.Plugins\.(\w+)',                   # Capacitor
        ],
    }

    # ── Constructor ───────────────────────────────────────────────

    def __init__(self, project_path: str, file_paths: list[str]):
        """
        project_path: absolute path to project root.
        file_paths:   project-relative paths of all files to analyse.
        """
        self.project_path = os.path.realpath(project_path)
        self.file_paths   = file_paths
        # Pre-build lookup structures for fast resolution
        self._path_set     = set(file_paths)
        self._basename_map = self._build_basename_map()

    def _build_basename_map(self) -> dict[str, list[str]]:
        """stem → [matching project-relative paths] for fast resolution."""
        bmap: dict[str, list[str]] = {}
        for p in self.file_paths:
            stem = os.path.splitext(os.path.basename(p))[0].lower()
            bmap.setdefault(stem, []).append(p)
        return bmap

    # ── Main entry point ─────────────────────────────────────────

    def analyze(self) -> dict:
        """
        Run full analysis and return the dependency data dict ready
        to embed in project_index.json under "cross_dependencies".

        Schema:
        {
          "graph": {
            "file.py": {
              "imports": ["other.py", "utils.js"],      # resolved project paths
              "semantic": {
                "css_classes": ["btn-primary"],
                "dom_ids":     ["header"],
                "api_endpoints": ["/api/v1/users"],
                "event_names": ["user:created"],
                "env_vars":  ["DATABASE_URL"],
                "rpc_calls": ["get_board"],
              }
            }
          },
          "reverse_graph": {
            "core/state.py": ["main.py", "core/phases/planning.py"]
          },
          "unresolved": {
            "web/js/app.js": ["eel.js", "https://fonts.googleapis.com"]
          },
          "semantic_index": {
            "css_classes":   {"btn-primary": ["web/js/app.js", "web/index.html"]},
            "dom_ids":       {"header":      ["web/js/app.js"]},
            "api_endpoints": {"/api/items":  ["main.py", "web/js/app.js"]},
            "event_names":   {},
            "env_vars":      {"DATABASE_URL": ["core/config.py"]},
            "rpc_calls":     {"get_board":    ["web/js/app.js"]},
          }
        }
        """
        graph:    dict[str, dict] = {}
        reverse:  dict[str, list[str]] = {}
        unresolved: dict[str, list[str]] = {}
        # Global index: semantic_value → [files that mention it]
        sem_index: dict[str, dict[str, list[str]]] = {
            k: {} for k in self._SEMANTIC_RULES
        }

        for rel in self.file_paths:
            if self._should_skip(rel):
                continue

            content = self._read(rel)
            if content is None:
                continue

            resolved, raw_unresolved = self._extract_imports(rel, content)
            semantic = self._extract_semantic(content)

            if resolved or semantic:
                graph[rel] = {"imports": resolved, "semantic": semantic}

            if raw_unresolved:
                unresolved[rel] = raw_unresolved

            # Build reverse graph
            for dep in resolved:
                reverse.setdefault(dep, [])
                if rel not in reverse[dep]:
                    reverse[dep].append(rel)

            # Build global semantic index
            for sem_type, values in semantic.items():
                for val in values:
                    sem_index[sem_type].setdefault(val, [])
                    if rel not in sem_index[sem_type][val]:
                        sem_index[sem_type][val].append(rel)

        return {
            "graph":          graph,
            "reverse_graph":  reverse,
            "unresolved":     unresolved,
            "semantic_index": sem_index,
        }

    # ── Import extraction ────────────────────────────────────────

    def _extract_imports(
        self, rel: str, content: str
    ) -> tuple[list[str], list[str]]:
        """
        Return (resolved_project_files, unresolved_external_refs).
        """
        ext  = os.path.splitext(rel)[1].lower()
        # Also match by lowercase filename for Makefile / Dockerfile etc.
        name = os.path.basename(rel).lower()

        raw_refs: list[str] = []

        for exts, patterns in self._IMPORT_RULES:
            # Check extension OR lowercase filename
            if ext in exts or name in exts:
                for pat in patterns:
                    raw_refs.extend(re.findall(pat, content, re.MULTILINE))

        resolved:   list[str] = []
        unresolved: list[str] = []

        for ref in dict.fromkeys(raw_refs):   # deduplicate, preserve order
            ref = ref.strip()
            if not ref:
                continue
            target = self._resolve(ref, rel)
            if target:
                if target not in resolved:
                    resolved.append(target)
            else:
                # Skip obvious externals / stdlib
                if not self._is_external(ref):
                    unresolved.append(ref)

        return resolved, unresolved

    def _resolve(self, ref: str, from_file: str) -> str | None:
        """
        Try to resolve a raw reference string to a project-relative path.

        Strategy (in order):
          1. Exact match in project file set
          2. Relative path resolution from the importing file's directory
          3. Module-to-path mapping (dots → slashes + common extensions)
          4. Basename / stem fuzzy match
        """
        from_dir = os.path.dirname(from_file)

        # 1. Direct exact match
        if ref in self._path_set:
            return ref

        # 2. Relative path resolution
        if ref.startswith(("./", "../", "/")):
            # Try with and without common extensions
            base = ref.lstrip("/")
            if not ref.startswith("/"):
                base = os.path.normpath(
                    os.path.join(from_dir, ref)
                ).replace("\\", "/")
            for ext in ("", ".py", ".js", ".ts", ".jsx", ".tsx", ".vue",
                        ".rb", ".go", ".rs", ".php", ".java", ".kt",
                        ".css", ".scss", ".html", ".htm"):
                candidate = base + ext
                if candidate in self._path_set:
                    return candidate
            # Also try as directory with index file
            for idx in ("index.js", "index.ts", "index.jsx", "index.tsx",
                        "__init__.py", "mod.rs"):
                candidate = (base.rstrip("/") + "/" + idx)
                if candidate in self._path_set:
                    return candidate
            return None

        # 3. Module path → file path (Python: core.state → core/state.py)
        module_path = ref.replace(".", "/").replace("\\", "/")
        for ext in (".py", ".js", ".ts", ".go", ".rb", ".php", ".java",
                    ".kt", ".rs", ".dart", ".swift"):
            candidate = module_path + ext
            if candidate in self._path_set:
                return candidate
        # With leading slash variants
        if "/" in module_path:
            # Try last two segments (handles package.module)
            parts = module_path.split("/")
            for depth in range(1, min(4, len(parts))):
                partial = "/".join(parts[-depth:])
                for ext in (".py", ".js", ".ts"):
                    for p in self.file_paths:
                        if p.endswith(partial + ext):
                            return p

        # 4. Stem-based fuzzy match
        stem = os.path.splitext(os.path.basename(ref.replace("\\", "/")))[0].lower()
        if stem:
            candidates = self._basename_map.get(stem, [])
            if len(candidates) == 1:
                return candidates[0]
            # If multiple matches, prefer one in the same directory tree
            if candidates:
                same_dir = [c for c in candidates if c.startswith(from_dir)]
                if same_dir:
                    return same_dir[0]

        return None

    @staticmethod
    def _is_external(ref: str) -> bool:
        """Return True if this looks like an external/stdlib reference."""
        return (
            ref.startswith(("http://", "https://", "//"))
            or ("/" not in ref and "." not in ref)  # bare name like 'os', 'sys', 'path'
            or ref.startswith(("@", "~"))             # npm scoped or alias
            or ref.startswith("node:")                # Node.js built-ins
        )

    # ── Semantic extraction ──────────────────────────────────────

    def _extract_semantic(self, content: str) -> dict[str, list[str]]:
        """Extract semantic cross-references from file content."""
        result: dict[str, list[str]] = {}
        for sem_type, patterns in self._SEMANTIC_RULES.items():
            values: set[str] = set()
            for pat in patterns:
                for match in re.findall(pat, content):
                    # CSS class= may contain multiple space-separated names
                    if sem_type == "css_classes" and " " in match:
                        values.update(match.split())
                    else:
                        values.add(match.strip())
            if values:
                result[sem_type] = sorted(values)
        return result

    # ── Helpers ──────────────────────────────────────────────────

    def _read(self, rel: str) -> str | None:
        try:
            abs_path = os.path.join(self.project_path, rel)
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                # Read up to 500 lines — enough to capture all imports
                # (usually at top) and avoid huge files bloating memory
                return "".join(f.readline() for _ in range(500))
        except Exception:
            return None

    @staticmethod
    def _should_skip(rel: str) -> bool:
        """Skip binary-ish and generated files."""
        skip_exts = {
            ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe",
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
            ".woff", ".woff2", ".ttf", ".eot",
            ".pdf", ".zip", ".tar", ".gz",
            ".lock",  # package-lock.json etc
        }
        ext = os.path.splitext(rel)[1].lower()
        return ext in skip_exts or rel.startswith((".tasks", ".git"))


def analyze_cross_deps(project_path: str, file_paths: list[str]) -> dict:
    """
    Convenience wrapper — runs CrossDepsAnalyzer and returns the result dict.
    Called from planning.py before Discovery so the model gets pre-computed deps.
    """
    return CrossDepsAnalyzer(project_path, file_paths).analyze()
