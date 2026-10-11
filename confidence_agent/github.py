"""Post (or update) the report as a single sticky comment on a pull request.

Uses the GitHub CLI (`gh`), which must be logged in locally or have GH_TOKEN
set in CI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .report import MARKER


def _gh(repo: Path, *args: str, input_text: str = None) -> str:
    return subprocess.run(
        ["gh", *args], cwd=repo, check=True, capture_output=True, text=True, input=input_text
    ).stdout


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


def _post(repo: Path, pr_number: int, body: str) -> str:
    ids = _gh(
        repo, "api", f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments", "--paginate",
        "--jq", f'.[] | select((.body // "") | contains("{MARKER}")) | .id',
    ).split()
    payload = json.dumps({"body": body})
    if ids:
        _gh(repo, "api", "-X", "PATCH", f"repos/{{owner}}/{{repo}}/issues/comments/{ids[0]}",
            "--input", "-", input_text=payload)
        return "updated"
    _gh(repo, "api", "-X", "POST", f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        "--input", "-", input_text=payload)
    return "created"
