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

from . import cache, config, gitutil, llm, mutate, report, runner, scoring, signals


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="confidence-agent",
        description="Score how much to trust an (AI-generated) change using tests, coverage, logs and traces.",
    )
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument("--base", default="main", help="branch or commit the change targets (default: main)")
    p.add_argument("--head", default="HEAD", help="branch or commit with the change (default: HEAD)")
    p.add_argument("--source", help="package/folder to measure coverage for (comma-separate several); changed "
                   "files outside it are not coverage-checked (default: [tool.confidence] source, else .)")
    p.add_argument("--tests", help="test folder (default: [tool.confidence] tests, else tests)")
    p.add_argument("--out", default="confidence-report", help="folder for report.md and result.json")
    p.add_argument("--no-memory", action="store_true",
                   help="skip per-test peak memory measurement (tracemalloc slows tests down)")
    p.add_argument("--no-mutation", action="store_true",
                   help="skip mutation testing of changed lines (it reruns tests once per mutant)")
    p.add_argument("--cache-dir", type=Path, default=None,
                   help="where to keep base-run results between runs (default: ~/.cache/confidence-agent)")
    p.add_argument("--no-cache", action="store_true", help="always rerun the base tests")
    p.add_argument("--no-llm", action="store_true", help="skip the plain-English summary")
    p.add_argument("--llm-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    p.add_argument("--llm-model", default=os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"))
    p.add_argument("--post-to-pr", type=int, metavar="NUMBER", help="post/update the report as a PR comment via gh")
    p.add_argument("--ack", default="", metavar="SIGNALS",
                   help="comma-separated deductions to show but not count, e.g. metrics,size "
                        "(on a PR, use labels like confidence-ack:metrics instead)")
    p.add_argument("--min-score", type=int,
                   help="exit 1 if the score is below this, for CI gates (default: [tool.confidence] min_score, else 0)")
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
    # Settings come from the base commit, so a pull request can't loosen its own checks.
    try:
        cfg = config.parse(gitutil.read_file(repo, base_sha, "pyproject.toml") or "")
    except config.ConfigError as exc:
        print(f"[confidence] error in [tool.confidence] at the base commit: {exc}")
        return 2
    args.source = args.source or cfg.source or "."
    args.tests = args.tests or cfg.tests or "tests"
    args.min_score = args.min_score if args.min_score is not None else (cfg.min_score or 0)
    diff = gitutil.read_diff(repo, base_sha, head_sha)

    tmp = Path(tempfile.mkdtemp(prefix="confidence-wt-"))
    runs = {}
    try:
        cache_dir = args.cache_dir or cache.default_dir()
        base_key = cache.key(base_sha, args.source, args.tests, not args.no_memory)
        if not args.no_cache:
            cached = cache.load(cache_dir, base_key)
            if cached is not None:
                print(f"[confidence] using cached results for base {base_sha[:7]}")
                runs["base"] = cached
        for label, sha in (("base", base_sha), ("head", head_sha)):
            if label in runs:
                continue
            wt = tmp / label
            gitutil.add_worktree(repo, sha, wt)
            print(f"[confidence] running tests on {label}...")
            runs[label] = runner.run_tests(
                wt, label, args.source, args.tests, track_memory=not args.no_memory, repo=repo
            )
            if label == "head":
                # Rerun tests that newly fail once, so one flaky test can't sink the score.
                base_failed = set(runs["base"].failed)
                newly = [t for t in runs["head"].failed if t not in base_failed]
                if newly:
                    print(f"[confidence] rerunning {len(newly)} newly failing test(s) to check for flakiness...")
                    runs["head"].rerun = runner.rerun_tests(wt, newly)
                limit = cfg.thresholds.max_mutants
                targets = signals.mutation_targets(diff, runs["head"].coverage, args.tests, args.source, cfg)
                if targets and limit > 0 and not args.no_mutation:
                    print(f"[confidence] mutation testing up to {limit} change(s) to changed lines...")
                    contexts = {p: runs["head"].coverage[p].get("contexts", {}) for p in targets}
                    runs["head"].mutation = mutate.run_mutations(
                        wt, targets, contexts, runner._import_paths(wt), limit, cfg.thresholds.mutant_timeout_s
                    )
            if label == "base" and not args.no_cache and runs["base"].tests:
                cache.save(cache_dir, base_key, runs["base"])
            gitutil.remove_worktree(repo, wt)
    finally:
        for label in ("base", "head"):
            gitutil.remove_worktree(repo, tmp / label)

    if not runs["head"].tests:
        print("[confidence] no test results from head run. pytest output:\n" + runs["head"].output_tail)

    sig = signals.compute(runs["base"], runs["head"], diff, args.tests, args.source, cfg)
    for warning in sig.warnings:
        print(f"[confidence] WARNING: {warning}")
    acks = {a.strip().lower(): "the command line" for a in args.ack.split(",") if a.strip()}
    notes = []
    if args.post_to_pr:
        from .github import acknowledgements
        labels, author = acknowledgements(repo, args.post_to_pr)
        for signal, who in labels.items():
            if who == author and not cfg.allow_self_ack:
                notes.append(f"Ignored confidence-ack:{signal}: added by the pull request's author "
                             f"(@{who}). Set allow_self_ack = true in [tool.confidence] to allow this.")
            else:
                acks[signal] = f"@{who}"
    for note in notes:
        print(f"[confidence] {note}")
    result = scoring.score(sig, cfg, acks)
    print(f"[confidence] score {result.value}/100 ({result.band})")

    summary = None
    if not args.no_llm:
        print(f"[confidence] asking {args.llm_model} at {args.llm_url} for a summary (can take a minute on CPU)...")
        summary = llm.summarize(args.llm_url, args.llm_model, result, sig)

    md = report.render(result, sig, base_sha, head_sha, summary, notes)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "result.json").write_text(
        json.dumps(
            {"score": result.value, "band": result.band,
             "deductions": [{"signal": s, "points": p, "reason": r} for s, p, r in result.deductions],
             "acknowledged": [{"signal": s, "points": p, "reason": r, "by": w} for s, p, r, w in result.acknowledged],
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
