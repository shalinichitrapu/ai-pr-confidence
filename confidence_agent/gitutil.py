"""Small git helpers: resolve refs, check out snapshots, read the diff."""

from __future__ import annotations

import io
import re
import subprocess
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        # quotePath=false: print non-ASCII file names as-is instead of quoted escapes.
        # errors="replace": a diff of a file that isn't UTF-8 mustn't crash the run.
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def _decode_source(data: bytes, path: str) -> str:
    """Python files honour their coding declaration; anything else is UTF-8, best effort."""
    if path.endswith(".py"):
        try:
            encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
            return data.decode(encoding)
        except (SyntaxError, LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", errors="replace")


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of unusual file names ("we\"ird.py")."""
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    simple = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
    out, text, i = bytearray(), path[1:-1], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            if text[i + 1] in simple:
                out.append(simple[text[i + 1]])
                i += 2
                continue
            if text[i + 1:i + 4].isdigit():  # \303\251: the UTF-8 bytes of a non-ASCII name
                out.append(int(text[i + 1:i + 4], 8))
                i += 4
                continue
        out += c.encode("utf-8")  # with core.quotePath=false, non-ASCII arrives as-is
        i += 1
    return out.decode("utf-8", "replace")


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def read_file(repo: Path, commit: str, path: str) -> Optional[str]:
    """A file's contents at a commit, or None if it doesn't exist there."""
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"], capture_output=True)
    return _decode_source(proc.stdout, path) if proc.returncode == 0 else None


def merge_base(repo: Path, base: str, head: str) -> str:
    return git(repo, "merge-base", base, head).strip()


def add_worktree(repo: Path, commit: str, path: Path) -> None:
    git(repo, "worktree", "add", "--detach", "--force", str(path), commit)


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
        capture_output=True,
    )


@dataclass
class Diff:
    added_lines: Dict[str, Set[int]] = field(default_factory=dict)
    files_changed: List[str] = field(default_factory=list)
    file_lines: Dict[str, int] = field(default_factory=dict)  # path -> lines added + removed
    renamed: Dict[str, str] = field(default_factory=dict)  # new path -> old path, as git detected
    lines_added: int = 0
    lines_removed: int = 0
    added_text: Dict[str, List[str]] = field(default_factory=dict)  # path -> added lines
    head_source: Dict[str, str] = field(default_factory=dict)  # changed .py file -> its text at head
    dependency_changes: List[str] = field(default_factory=list)  # "path: added pkg >=1" etc.
    pytest_config_changes: List[str] = field(default_factory=list)  # "pyproject.toml: addopts" etc.
    removed_lines: Dict[str, Set[int]] = field(default_factory=dict)  # old path -> base line numbers removed
    base_source: Dict[str, str] = field(default_factory=dict)  # .py file with removals -> its text at base
    removed_text: Dict[str, List[str]] = field(default_factory=dict)  # path -> removed lines

    @property
    def size(self) -> int:
        return self.lines_added + self.lines_removed


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _numstat(repo: Path, base: str, head: str) -> List[Tuple[str, str, str, str]]:
    """(added, removed, path, old_path) per file; "-" counts for binary files.

    old_path is set only for files git detected as renamed or copied.

    Uses -z so a renamed file reports its new path rather than git's
    display form "dir/{old => new}".
    """
    fields = git(repo, "diff", "--numstat", "-z", base, head).split("\0")
    out, i = [], 0
    while i < len(fields) and fields[i]:
        added, removed, path = fields[i].split("\t", 2)
        old = ""
        if path:
            i += 1
        else:  # rename or copy: old and new paths follow as separate fields
            old, path = fields[i + 1], fields[i + 2]
            i += 3
        out.append((added, removed, path, old))
    return out


def read_diff(repo: Path, base: str, head: str) -> Diff:
    """Parse `git diff base head` into the new-side line numbers that were added."""
    diff = Diff()
    for added, removed, path, old in _numstat(repo, base, head):
        diff.files_changed.append(path)
        if old:
            diff.renamed[path] = old
        if added != "-":
            diff.lines_added += int(added)
            diff.lines_removed += int(removed)
            diff.file_lines[path] = int(added) + int(removed)

    current = old = None
    in_header = False
    # Fixed prefixes and no external diff tool, whatever the user's git config says
    # (diff.noprefix, diff.external, color). Split on "\n" only: str.splitlines
    # would also break lines at form feeds and other separators inside them.
    patch = git(repo, "diff", "--unified=0", "--no-color", "--no-ext-diff",
                "--src-prefix=a/", "--dst-prefix=b/", base, head)
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_header, current, old = True, None, None
            continue
        # git appends a tab after file names with spaces
        if in_header and line.startswith("--- "):
            source = _unquote(line[4:].rstrip("\t"))
            old = source[2:] if source.startswith("a/") else None
            continue
        if in_header and line.startswith("+++ "):
            target = _unquote(line[4:].rstrip("\t"))
            current = target[2:] if target.startswith("b/") else None
            if current:
                diff.added_lines.setdefault(current, set())
            continue
        m = _HUNK.match(line)
        if m:
            in_header = False
            if current:
                start = int(m.group(3))
                count = int(m.group(4)) if m.group(4) is not None else 1
                diff.added_lines[current].update(range(start, start + count))
            if old:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count:
                    diff.removed_lines.setdefault(old, set()).update(range(start, start + count))
            continue
        if in_header:
            continue
        if line.startswith("+") and current:
            diff.added_text.setdefault(current, []).append(line[1:])
        elif line.startswith("-") and old:
            diff.removed_text.setdefault(old, []).append(line[1:])
    for path in diff.added_lines:
        if path.endswith(".py"):
            text = read_file(repo, head, path)
            if text is not None:
                diff.head_source[path] = text
    from .review_scope import all_dependency_changes, is_dependency_file, pytest_settings
    for path in diff.files_changed:
        before = pytest_settings(path, read_file(repo, base, diff.renamed.get(path, path)) or "")
        after = pytest_settings(path, read_file(repo, head, path) or "")
        if before is not None and before != after:
            keys = sorted(k for k in set(before) | set(after or {}) if before.get(k) != (after or {}).get(k))
            diff.pytest_config_changes.append(f"{path}: {', '.join(keys)}")
    diff.dependency_changes = all_dependency_changes(
        (path, read_file(repo, base, diff.renamed.get(path, path)), read_file(repo, head, path))
        for path in diff.files_changed if is_dependency_file(path)
    )
    # Every changed Python file's base version (not only ones with removed lines):
    # comparing a test's code before and after needs it even for add-only edits.
    for path in {*diff.removed_lines, *(diff.renamed.get(p, p) for p in diff.added_lines)}:
        if path.endswith(".py"):
            text = read_file(repo, base, path)
            if text is not None:
                diff.base_source[path] = text
    return diff
