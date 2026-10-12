"""Check the score against what actually happened to past changes.

    confidence-calibrate --repo . --branch main --limit 30 --source my_package

Scores recent changes on a branch (each commit on its first-parent line: a merge
commit covers everything it merged, any other commit covers itself), finds the
ones that were later reverted, and reports how
often each band and each kind of deduction was followed by a revert. That's the
evidence for tuning [tool.confidence.weights]: a deduction whose PRs get
reverted far more often than average deserves more weight, and one with no
lift deserves less.

Reverts are found from git's own "This reverts commit <sha>" messages. Outcomes
git can't see (incidents, hotfixes) can be supplied with --outcomes, a CSV of
"sha,outcome" rows where outcome is "bad" or "good".
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from . import cli, gitutil

REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


@dataclass
class Change:
    sha: str  # the commit on the branch (merge commit, or the squashed commit)
    base: str
    head: str
    subject: str
    commits: Set[str] = field(default_factory=set)  # every commit the change brought in
    score: Optional[int] = None
    band: str = ""
    signals: List[str] = field(default_factory=list)  # deduction names
    outcome: str = "good"  # "bad" if reverted or listed as bad
    skipped: str = ""  # why it wasn't scored, when it wasn't an error
    why: str = ""


def changes(repo: Path, branch: str, limit: int) -> List[Change]:
    """Recent changes on the branch, newest first.

    Every commit on the first-parent line is one change: a merge covers what it
    merged, and a direct or squashed commit covers itself. Histories that mix
    both styles are scored in full. Revert commits aren't changes of their own:
    they're how a change's outcome is found. The root commit is skipped.
    """
    log = gitutil.git(repo, "log", "--first-parent", "--format=%H %P%x00%s%x00%b%x01", branch)
    found = []
    for record in log.split("\x01"):
        if not record.strip():
            continue
        ids, subject, body = (record.strip("\n").split("\0") + ["", ""])[:3]
        if subject.startswith("Merge pull request") and body.strip():
            subject = body.strip().splitlines()[0]  # GitHub puts the PR title here
        sha, *parents = ids.split()
        if not parents:
            continue  # the root commit has nothing to compare against
        # A revert commit says so in its subject, or its body starts with git's line.
        # A squash-merged PR whose body merely lists an internal revert is a real change.
        if subject.startswith(('Revert "', "Reapply \"")) or body.lstrip().startswith("This reverts commit"):
            continue
        found.append((sha, parents, subject))
        if len(found) == limit:
            break
    out = []
    for sha, parents, subject in found:
        base, head = (parents[0], parents[1]) if len(parents) == 2 else (parents[0], sha)
        commits = set(gitutil.git(repo, "rev-list", f"{base}..{head}").split()) | {sha}
        out.append(Change(sha, base, head, subject, commits))
    return out


def reverted(repo: Path, branch: str) -> Dict[str, str]:
    """{reverted sha: the commit that reverted it}, from the whole branch history."""
    log = gitutil.git(repo, "log", "--format=%H%x00%B%x01", branch)
    out = {}
    for entry in log.split("\x01"):
        sha, _, body = entry.strip().partition("\0")
        for target in REVERTS.findall(body):
            out[target] = sha
    return out


def _is_reverted(change: Change, reverts: Dict[str, str]) -> Optional[str]:
    """The commit that reverted part of this change, if any.

    A revert made inside the change itself (a work-in-progress commit undone
    before merging) doesn't count.
    """
    for target, by in reverts.items():
        if by in change.commits:
            continue
        if any(c.startswith(target) or target.startswith(c) for c in change.commits):
            return by
    return None


def _outcomes(path: Optional[Path]) -> Dict[str, str]:
    """{sha (lowercase, at least 7 hex characters): outcome}. Other rows are skipped with a message."""
    if path is None:
        return {}
    labels = {}
    with path.open(newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            sha, outcome = row[0].strip().lower(), row[1].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
                print(f"[calibrate] skipping outcome row {row!r}: the sha must be 7-40 hex characters")
                continue
            if outcome not in ("bad", "good"):
                print(f"[calibrate] skipping outcome row {row!r}: the outcome must be bad or good")
                continue
            labels[sha] = outcome
    return labels


def report(items: List[Change]) -> str:
    scored = [c for c in items if c.score is not None]
    bad = [c for c in scored if c.outcome == "bad"]
    base_rate = len(bad) / len(scored) if scored else 0.0
    md = ["# Confidence calibration", "",
          f"{len(scored)} changes scored, {len(bad)} later reverted or marked bad "
          f"({base_rate:.0%}).", "",
          "## By band", "", "| Band | Changes | Reverted or bad | Rate |", "|---|---:|---:|---:|"]
    for band in ("High", "Medium", "Low"):
        group = [c for c in scored if c.band == band]
        n_bad = sum(c.outcome == "bad" for c in group)
        rate = f"{n_bad / len(group):.0%}" if group else "–"
        md.append(f"| {band} | {len(group)} | {n_bad} | {rate} |")
    md += ["", "A useful score has a higher rate in Low than in High.", "",
           "## By deduction", "",
           "Lift is how much more often changes with this deduction went bad than changes overall "
           "(1.0 = no difference).", "",
           "| Deduction | Changes with it | Reverted or bad | Lift |", "|---|---:|---:|---:|"]
    names = sorted({s for c in scored for s in c.signals})
    for name in names:
        group = [c for c in scored if name in c.signals]
        n_bad = sum(c.outcome == "bad" for c in group)
        # With no bad outcomes at all there's nothing to compare against: unknown, not 0.
        lift = f"{(n_bad / len(group)) / base_rate:.1f}" if base_rate and group else "–"
        md.append(f"| {name} | {len(group)} | {n_bad} | {lift} |")
    md += ["", "## Changes", "", "| Commit | Subject | Score | Outcome |", "|---|---|---:|---|"]
    for c in items:
        score = (c.skipped or "error") if c.score is None else f"{c.score} ({c.band})"
        outcome = c.outcome + (f" ({c.why})" if c.why else "")
        subject = " ".join(c.subject[:60].split()).replace("|", "\\|").replace("<", "&lt;")
        md.append(f"| `{c.sha[:7]}` | {subject} | {score} | {outcome} |")
    md += ["", "_With few bad outcomes these numbers are noisy; treat them as a direction, not a fit._"]
    return "\n".join(md) + "\n"


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="confidence-calibrate", description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=".")
    p.add_argument("--branch", default="main")
    p.add_argument("--limit", type=_positive, default=20, help="how many recent changes to score (default 20)")
    p.add_argument("--outcomes", type=Path, help='CSV of "sha,bad|good" rows for outcomes git can\'t see')
    p.add_argument("--out", default="confidence-calibration", help="folder for calibration.md and .csv")
    p.add_argument("--source")
    p.add_argument("--tests")
    p.add_argument("--full", action="store_true",
                   help="also measure memory and run mutation testing (slower; off by default here)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    items = changes(repo, args.branch, args.limit)
    reverts = reverted(repo, args.branch)
    labels = _outcomes(args.outcomes)
    for i, change in enumerate(items, 1):
        print(f"[calibrate] {i}/{len(items)} {change.sha[:7]} {change.subject[:60]}")
        by = _is_reverted(change, reverts)
        if by:
            change.outcome, change.why = "bad", f"reverted by {by[:7]}"
        # An outcome may name the merge commit or any commit the change brought in.
        label = next((v for k, v in labels.items() if any(c.startswith(k) for c in change.commits)), None)
        if label:
            change.outcome, change.why = label, "from --outcomes"
        with tempfile.TemporaryDirectory(prefix="confidence-calibrate-") as out:
            agent_args = ["--repo", str(repo), "--base", change.base, "--head", change.head,
                          "--no-llm", "--out", out]
            for flag in ("source", "tests"):
                if getattr(args, flag):
                    agent_args += [f"--{flag}", getattr(args, flag)]
            if not args.full:
                agent_args += ["--no-memory", "--no-mutation"]
            try:
                cli.main(agent_args)
                result_file = Path(out) / "result.json"
                if not result_file.exists():
                    print(f"[calibrate] skipping {change.sha[:7]}: it changes nothing to score")
                    change.skipped = "nothing to score"
                    continue
                result = json.loads(result_file.read_text())
            except (Exception, SystemExit) as exc:  # one change that can't be scored shouldn't stop the run
                print(f"[calibrate] could not score {change.sha[:7]}: {exc}")
                continue
        change.score, change.band = result["score"], result["band"]
        # Group by the kind of deduction (the same keys as the weights), not the display name.
        change.signals = sorted({d.get("key") or d["signal"] for d in result["deductions"]})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.md").write_text(report(items), encoding="utf-8")
    with (out / "calibration.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sha", "subject", "score", "band", "deductions", "outcome", "why"])
        for c in items:
            writer.writerow([c.sha, c.subject, c.score, c.band, ";".join(c.signals), c.outcome, c.why])
    print(f"[calibrate] wrote {out / 'calibration.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
