"""Text the pull request controls can't format the report, notify people or block posting."""

import re
import subprocess

from confidence_agent import github, report
from confidence_agent.runner import SECRET_ENV, optional_flags, run_pytest
from confidence_agent.scoring import score
from confidence_agent.signals import Signals

EVIL = "@someone [Approved](https://evil.example) ![p](https://evil.example/t.png) **ok**"


def test_span_names_and_config_warnings_cant_add_links_images_or_mentions():
    s = Signals(tests_total=1, otel_enabled=True, latency_regressions={EVIL: {"before_ms": 1.0, "after_ms": 3.0}}, warnings=[f"unknown key {EVIL}"])
    md = report.render(score(s), s, "a" * 40, "b" * 40, None)
    md = re.sub(r"`[^`]*`", "", md)  # inside code spans nothing renders as Markdown
    for forged in (r"(?<!\\)\]\(", r"(?<!\\)!(?<!\\)\[", r"(?<!\\)\*\*ok", "@someone", "https://evil"):
        assert not re.search(forged, md), forged


def test_very_long_values_are_clipped():
    assert len(report._code("x" * 70000)) < 400


def test_an_oversized_comment_is_clipped_so_github_accepts_it(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(github, "_post", lambda repo, pr, body: sent.append(body) or "created")
    github.post_comment(tmp_path, 1, report.MARKER + "x" * 100000)
    assert len(sent[0]) < 65536 and report.MARKER in sent[0]


def test_a_local_run_updates_the_comment_ci_posted(monkeypatch, tmp_path):
    def gh(repo, *args, input_text=None):
        if args[:2] == ("api", "user"):
            return "shalini\n"
        if "--paginate" in args:
            jq = args[args.index("--jq") + 1]
            assert '"github-actions[bot]"' in jq and '"shalini"' in jq
            return "42\n"
        return ""
    monkeypatch.setattr(github, "_gh", gh)
    assert github._post(tmp_path, 1, "body") == "updated"


def test_job_files_are_kept_from_tests():
    for name in ("GITHUB_STEP_SUMMARY", "GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH"):
        assert SECRET_ENV.search(name)
    assert not SECRET_ENV.search("GITHUB_SHA")


def test_the_retry_keeps_the_no_shuffle_flags(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd) or
                        subprocess.CompletedProcess(cmd, 4 if len(seen) == 1 else 0))
    run_pytest(["pytest"], ["-p", "no:randomly", "-n", "0"])
    assert seen[-1] == ["pytest", "-p", "no:randomly"]
    assert "-p" in optional_flags()


def test_labels_added_by_a_bot_dont_acknowledge():
    from confidence_agent.cli import label_acks
    acks, notes = label_acks({"size": "github-actions[bot]", "metrics": "reviewer", "logs": "author"}, "author", False)
    assert acks == {"metrics": "reviewer"} and len(notes) == 2 and "automated account" in notes[0]
