"""Command line entry point.

    python -m confidence_agent --base main --head my-branch --source studybuddy
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import gitutil, llm, report, runner, scoring, signals


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="confidence-agent",
        description="Score how much to trust an (AI-generated) change using tests, coverage, logs and traces.",
    )
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument("--base", default="main", help="branch or commit the change targets (default: main)")
    p.add_argument("--head", default="HEAD", help="branch or commit with the change (default: HEAD)")
    p.add_argument("--source", default=".", help="package/folder to measure coverage for; changed files outside it are not coverage-checked")
    p.add_argument("--tests", default="tests", help="test folder (default: tests)")
    p.add_argument("--out", default="confidence-report", help="folder for report.md and result.json")
    p.add_argument("--no-llm", action="store_true", help="skip the plain-English summary")
    p.add_argument("--llm-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    p.add_argument("--llm-model", default=os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"))
    p.add_argument("--post-to-pr", type=int, metavar="NUMBER", help="post/update the report as a PR comment via gh")
    p.add_argument("--min-score", type=int, default=0, help="exit 1 if the score is below this (for CI gates)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    head_sha = gitutil.resolve(repo, args.head)
    base_sha = gitutil.merge_base(repo, gitutil.resolve(repo, args.base), head_sha)
    if base_sha == head_sha:
        print("[confidence] head has no changes relative to base; nothing to score.")
        return 0

    print(f"[confidence] base {base_sha[:7]}  head {head_sha[:7]}")
    diff = gitutil.read_diff(repo, base_sha, head_sha)

    tmp = Path(tempfile.mkdtemp(prefix="confidence-wt-"))
    runs = {}
    try:
        for label, sha in (("base", base_sha), ("head", head_sha)):
            wt = tmp / label
            gitutil.add_worktree(repo, sha, wt)
            print(f"[confidence] running tests on {label}...")
            runs[label] = runner.run_tests(wt, label, args.source, args.tests)
            gitutil.remove_worktree(repo, wt)
    finally:
        for label in ("base", "head"):
            gitutil.remove_worktree(repo, tmp / label)

    if not runs["head"].tests:
        print("[confidence] no test results from head run. pytest output:\n" + runs["head"].output_tail)

    sig = signals.compute(runs["base"], runs["head"], diff, args.tests, args.source)
    result = scoring.score(sig)
    print(f"[confidence] score {result.value}/100 ({result.band})")

    summary = None
    if not args.no_llm:
        print(f"[confidence] asking {args.llm_model} at {args.llm_url} for a summary (can take a minute on CPU)...")
        summary = llm.summarize(args.llm_url, args.llm_model, result, sig)

    md = report.render(result, sig, base_sha, head_sha, summary)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "result.json").write_text(
        json.dumps(
            {"score": result.value, "band": result.band,
             "deductions": [{"signal": s, "points": p, "reason": r} for s, p, r in result.deductions],
             "passed_checks": result.passed_checks, "signals": sig.to_dict(), "summary": summary,
             "base": base_sha, "head": head_sha},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[confidence] wrote {out / 'report.md'}")

    if args.post_to_pr:
        from .github import post_comment
        print(f"[confidence] PR #{args.post_to_pr}: comment {post_comment(repo, args.post_to_pr, md)}")

    return 1 if result.value < args.min_score else 0


if __name__ == "__main__":
    sys.exit(main())
