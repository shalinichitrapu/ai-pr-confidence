"""Post (or update) the report as a single sticky comment on a pull request.

Uses the GitHub CLI (`gh`), which must be logged in locally or have GH_TOKEN
set in CI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from .report import MARKER

ACK_PREFIX = "confidence-ack:"


def _gh(repo: Path, *args: str, input_text: str = None) -> str:
    return subprocess.run(
        ["gh", *args], cwd=repo, check=True, capture_output=True, text=True, input=input_text
    ).stdout


def acknowledgements(repo: Path, pr_number: int) -> Tuple[Dict[str, str], Optional[str]]:
    """Deductions acknowledged with labels like "confidence-ack:metrics".

    Returns ({signal: login of whoever added the label}, the PR author's login).
    Labels removed later don't count. Never raises: on any error, nothing is
    acknowledged.
    """
    try:
        author = _gh(repo, "pr", "view", str(pr_number), "--json", "author", "--jq", ".author.login").strip()
        events = _gh(
            repo, "api", f"repos/{{owner}}/{{repo}}/issues/{pr_number}/events", "--paginate",
            "--jq", '.[] | select(.event == "labeled" or .event == "unlabeled") '
                    '| [.event, .label.name, .actor.login] | @tsv',
        )
    except (subprocess.CalledProcessError, OSError):
        return {}, None
    acks: Dict[str, str] = {}
    for line in events.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1].lower().startswith(ACK_PREFIX):
            continue
        event, label, actor = parts
        signal = label[len(ACK_PREFIX):].strip().lower()
        if event == "labeled":
            acks[signal] = actor
        else:
            acks.pop(signal, None)
    return acks, author or None


def post_comment(repo: Path, pr_number: int, body: str) -> str:
    """Create or update the comment. Returns what happened; never raises.

    Pull requests from forks get a read-only token, so posting can be
    refused. The score is still valid, so that must not fail the check.
    """
    try:
        return _post(repo, pr_number, body)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = (getattr(exc, "stderr", "") or str(exc)).strip().splitlines()
        reason = detail[-1] if detail else type(exc).__name__
        return f"not posted ({reason}); the report is in the job summary and confidence-report/"


def _me(repo: Path) -> str:
    """The account the comment is posted as. The Actions token can't read /user; it posts as github-actions[bot]."""
    try:
        return _gh(repo, "api", "user", "--jq", ".login").strip() or "github-actions[bot]"
    except (subprocess.CalledProcessError, OSError):
        return "github-actions[bot]"


def _post(repo: Path, pr_number: int, body: str) -> str:
    # Only our own earlier comment is updated: anyone can write a comment containing
    # the marker, and updating theirs would put the report under their name.
    me = json.dumps(_me(repo))
    ids = _gh(
        repo, "api", f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments", "--paginate",
        "--jq", f'.[] | select(.user.login == {me} and ((.body // "") | contains("{MARKER}"))) | .id',
    ).split()
    payload = json.dumps({"body": body})
    if ids:
        _gh(repo, "api", "-X", "PATCH", f"repos/{{owner}}/{{repo}}/issues/comments/{ids[-1]}",
            "--input", "-", input_text=payload)
        return "updated"
    _gh(repo, "api", "-X", "POST", f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        "--input", "-", input_text=payload)
    return "created"
