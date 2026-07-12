"""
repo_wiki.py — repo → structured wiki generator (Phase 3, the deepwiki-open pattern,
done locally with no external service).

deepwiki-open is a Next.js app that reads a repo and asks a hosted model to write
a wiki. We don't import it — same closed-loop principle as the rest of Aegis:
express the pattern with local primitives. Here that's (1) a dependency-free repo
*digest* (file tree + the files that actually explain a project — READMEs,
manifests, entrypoints) and (2) a few targeted local-model passes that turn the
digest into an Overview / Architecture / Module-guide markdown wiki, saved under
data/wikis/.

Guarded — a scan or a model hiccup returns a helpful error, never crashes.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

WIKIS_DIR = os.path.join(DATA_DIR, "wikis")

# Directories that never help explain a project (deps, build output, VCS, data).
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "venv", ".venv", "env", "node_modules", "__pycache__",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages",
    ".idea", ".vscode", "coverage", "htmlcov", ".next", ".turbo", ".cache",
    "data", "models", "fastembed_cache", "chroma", "target", "vendor", ".gradle",
}
# Binary / bulky extensions we never read.
_SKIP_EXT = {
    ".gguf", ".bin", ".pt", ".pth", ".onnx", ".safetensors", ".h5",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".mp4", ".mp3", ".wav", ".mov", ".webm",
    ".db", ".sqlite", ".sqlite3", ".lock", ".map", ".min.js", ".min.css",
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".class", ".o", ".a",
}
# Files whose content is worth feeding to the model verbatim (truncated).
_KEY_FILES = (
    "readme", "readme.md", "readme.rst", "readme.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "gemfile", "composer.json", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "makefile", "main.py", "app.py", "__main__.py",
    "index.js", "index.ts", "main.go", "main.rs", "cli.py",
)

_MAX_TREE = 500          # tree entries shown to the model
_MAX_FILE_CHARS = 3500   # per key-file content cap
_MAX_DIGEST_CHARS = 14000  # total digest cap fed to the model


def _lang_of(ext: str) -> Optional[str]:
    return {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
        ".php": "PHP", ".c": "C", ".cpp": "C++", ".h": "C/C++", ".cs": "C#",
        ".sh": "Shell", ".ps1": "PowerShell", ".html": "HTML", ".css": "CSS",
        ".sql": "SQL", ".kt": "Kotlin", ".swift": "Swift", ".scala": "Scala",
    }.get(ext.lower())


def scan_repo(path: str) -> Dict[str, Any]:
    """Walk a repo into a compact digest: tree, language mix, and key-file text."""
    root = os.path.abspath(os.path.expanduser((path or "").strip()))
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")

    tree: List[str] = []
    langs: Dict[str, int] = {}
    key_files: List[Tuple[str, str]] = []
    truncated = False
    n_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in _SKIP_EXT or fn.startswith("."):
                continue
            n_files += 1
            rel = os.path.normpath(os.path.join(rel_dir, fn)) if rel_dir != "." else fn
            rel = rel.replace("\\", "/")
            if len(tree) < _MAX_TREE:
                tree.append(rel)
            else:
                truncated = True
            lang = _lang_of(ext)
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
            if fn.lower() in _KEY_FILES and len(key_files) < 24:
                try:
                    with open(os.path.join(dirpath, fn), "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read(_MAX_FILE_CHARS + 1)
                    if len(content) > _MAX_FILE_CHARS:
                        content = content[:_MAX_FILE_CHARS] + "\n… (truncated)"
                    key_files.append((rel, content))
                except Exception:
                    pass

    return {
        "root": root,
        "name": os.path.basename(root) or root,
        "tree": tree,
        "tree_truncated": truncated,
        "file_count": n_files,
        "langs": dict(sorted(langs.items(), key=lambda kv: kv[1], reverse=True)),
        "key_files": key_files,
    }


def _digest_text(scan: Dict[str, Any], include_files: bool = True) -> str:
    parts = [f"Repository: {scan['name']}",
             f"Files: {scan['file_count']}",
             "Languages: " + (", ".join(f"{k} ({v})" for k, v in scan["langs"].items()) or "n/a"),
             "", "File tree:", "\n".join(scan["tree"])]
    if scan.get("tree_truncated"):
        parts.append("… (tree truncated)")
    if include_files and scan["key_files"]:
        parts.append("\nKey files:")
        for rel, content in scan["key_files"]:
            parts.append(f"\n=== {rel} ===\n{content}")
    text = "\n".join(parts)
    return text[:_MAX_DIGEST_CHARS]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("._-")
    return s or "wiki"


async def generate_wiki(path: str, model_spec: str, owner: str = "") -> Dict[str, Any]:
    """Scan a repo and write a structured markdown wiki with the local model."""
    if not (model_spec or "").strip():
        return {"ok": False, "error": "no model selected — add one in Settings first."}
    try:
        scan = scan_repo(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not scan["tree"]:
        return {"ok": False, "error": "no source files found to document."}

    from src.recipes import _model_generate
    t0 = time.time()
    digest = _digest_text(scan, include_files=True)
    tree_only = _digest_text(scan, include_files=False)

    async def _section(system: str, user: str) -> str:
        try:
            out = await _model_generate(model_spec, system, user, owner)
            return (out or "").strip()
        except Exception as e:
            logger.debug(f"wiki section failed: {e}")
            return f"_(section generation failed: {e})_"

    overview = await _section(
        "You are a senior engineer writing developer documentation. Be accurate and "
        "concise; only state what the provided material supports. Use markdown, no top-level heading.",
        f"From this repository digest, write an **Overview**: what the project is, what "
        f"problem it solves, and its tech stack. 1–3 short paragraphs.\n\n{digest}")

    architecture = await _section(
        "You are a senior engineer. Write accurate markdown, no top-level heading.",
        f"From this repository digest, describe the **Architecture**: the main components, "
        f"how they fit together, entrypoints, and (if visible) the data flow. Use short "
        f"paragraphs and/or bullets.\n\n{digest}")

    modules = await _section(
        "You are a senior engineer writing a codebase tour. Accurate markdown, no top-level heading.",
        f"From this file tree, write a **Module guide**: a bulleted tour of the important "
        f"directories/files and what each is responsible for. Group related paths. Don't invent "
        f"files that aren't listed.\n\n{tree_only}")

    langs = ", ".join(f"{k} ({v})" for k, v in scan["langs"].items()) or "n/a"
    tree_block = "\n".join(scan["tree"][:200])
    if scan.get("tree_truncated") or len(scan["tree"]) > 200:
        tree_block += "\n… (truncated)"
    from datetime import date
    md = (
        f"# {scan['name']} — Wiki\n\n"
        f"_{scan['file_count']} files · {langs}_\n\n"
        f"## Overview\n\n{overview}\n\n"
        f"## Architecture\n\n{architecture}\n\n"
        f"## Module guide\n\n{modules}\n\n"
        f"## File tree\n\n```\n{tree_block}\n```\n\n"
        f"---\n_Generated locally by Aegis on {date.today().isoformat()} "
        f"using `{model_spec}`._\n"
    )

    saved_path = None
    try:
        os.makedirs(WIKIS_DIR, exist_ok=True)
        saved_path = os.path.join(WIKIS_DIR, _slug(scan["name"]) + ".md")
        with open(saved_path, "w", encoding="utf-8") as fh:
            fh.write(md)
    except Exception as e:
        logger.debug(f"wiki save failed: {e}")

    result = {
        "ok": True, "name": scan["name"], "markdown": md, "saved_path": saved_path,
        "model": model_spec, "file_count": scan["file_count"],
        "seconds": round(time.time() - t0, 1),
    }
    try:
        from src import tracing
        tracing.record(kind="wiki", model=model_spec, workload="repo_wiki",
                       latency_ms=int((result["seconds"]) * 1000),
                       prompt=f"wiki: {scan['root']}", response=md[:4000])
    except Exception:
        pass
    return result


# ── saved-wiki store ──────────────────────────────────────────────────────────
def list_wikis() -> List[Dict[str, Any]]:
    out = []
    try:
        for fn in sorted(os.listdir(WIKIS_DIR)):
            if fn.endswith(".md"):
                fp = os.path.join(WIKIS_DIR, fn)
                out.append({"name": fn[:-3], "file": fn,
                            "modified": os.path.getmtime(fp),
                            "size": os.path.getsize(fp)})
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"list_wikis failed: {e}")
    return out


def get_wiki(name: str) -> Optional[str]:
    fp = os.path.join(WIKIS_DIR, _slug(name) + ".md")
    if not os.path.abspath(fp).startswith(os.path.abspath(WIKIS_DIR)):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None
