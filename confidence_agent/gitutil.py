"""Small git helpers: resolve refs, check out snapshots, read the diff."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        # quotePath=false: print non-ASCII file names as-is instead of quoted escapes.
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


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
    removed_text: Dict[str, List[str]] = field(default_factory=dict)  # path -> removed lines

    @property
    def size(self) -> int:
        return self.lines_added + self.lines_removed


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


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
    for line in git(repo, "diff", "--unified=0", "--no-color", base, head).splitlines():
        if line.startswith("diff --git "):
            in_header, current, old = True, None, None
            continue
        # git appends a tab after file names with spaces
        if in_header and line.startswith("--- "):
            source = line[4:].rstrip("\t")
            old = source[2:] if source.startswith("a/") else None
            continue
        if in_header and line.startswith("+++ "):
            target = line[4:].rstrip("\t")
            current = target[2:] if target.startswith("b/") else None
            if current:
                diff.added_lines.setdefault(current, set())
            continue
        m = _HUNK.match(line)
        if m:
            in_header = False
            if current:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                diff.added_lines[current].update(range(start, start + count))
            continue
        if in_header:
            continue
        if line.startswith("+") and current:
            diff.added_text.setdefault(current, []).append(line[1:])
        elif line.startswith("-") and old:
            diff.removed_text.setdefault(old, []).append(line[1:])
    return diff
