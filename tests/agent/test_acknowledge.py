"""Reviewers can acknowledge a deduction: it stays visible but stops counting."""

import json
import subprocess

from confidence_agent import cli, github
from confidence_agent.config import parse
from confidence_agent.report import render
from confidence_agent.scoring import score
from confidence_agent.signals import Signals


def signals():
    return Signals(tests_total=3, metrics_enabled=True, code_diff_lines=900,
                   metric_regressions={"c": {"test": "t", "kind": "counter", "before": 1, "after": 5}})


def test_acknowledged_deductions_are_shown_but_not_counted():
    r = score(signals(), acks={"Metrics": "@lead"})
    assert r.value == 95  # only Size (-5) still counts
    assert [d[0] for d in r.deductions] == ["Size"]
    assert r.acknowledged == [("Metrics", 10, "1 metric(s) grew at least 2× in the same test", "@lead")]
    md = render(r, signals(), "a" * 40, "b" * 40, None)
    assert "| Metrics | 0 (−10 acknowledged by @lead) |" in md


def test_unknown_acks_change_nothing():
    assert score(signals(), acks={"latency": "@x"}).value == 85


def fake_gh(monkeypatch, author, events):
    def run(cmd, **kw):
        if cmd[1:3] == ["pr", "view"]:
            out = author + "\n"
        else:
            out = "".join("\t".join(e) + "\n" for e in events)
        return subprocess.CompletedProcess(cmd, 0, stdout=out)
    monkeypatch.setattr(github.subprocess, "run", run)


def test_labels_are_read_with_who_added_them(monkeypatch, tmp_path):
    fake_gh(monkeypatch, "author", [
        ("labeled", "confidence-ack:metrics", "lead"),
        ("labeled", "bug", "lead"),
        ("labeled", "confidence-ack:size", "lead"),
        ("unlabeled", "confidence-ack:size", "lead"),
        ("labeled", "Confidence-Ack:Dependencies", "author"),
    ])
    acks, author = github.acknowledgements(tmp_path, 7)
    assert acks == {"metrics": "lead", "dependencies": "author"}
    assert author == "author"


def test_gh_failure_means_no_acknowledgements(monkeypatch, tmp_path):
    def fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(github.subprocess, "run", fail)
    assert github.acknowledgements(tmp_path, 7) == ({}, None)


def test_allow_self_ack_setting():
    assert parse("").allow_self_ack is False
    assert parse("[tool.confidence]\nallow_self_ack = true\n").allow_self_ack is True


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text("def f():\n    return 1\n")
    (repo / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    (repo / "requirements.txt").write_text("leftpad==1.0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add a dependency")
    return repo


def run_cli(repo, out, *extra):
    cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--source", "app",
              "--no-llm", "--no-memory", "--out", str(out), *extra])
    return json.loads((out / "result.json").read_text())


def test_cli_ack_flag(tmp_path):
    repo = make_repo(tmp_path)
    plain = run_cli(repo, tmp_path / "plain")
    acked = run_cli(repo, tmp_path / "acked", "--ack", "dependencies")
    assert plain["score"] == 95
    assert acked["score"] == 100
    assert acked["acknowledged"][0]["by"] == "the command line"


def test_self_ack_by_label_is_ignored(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(github, "acknowledgements", lambda repo, pr: ({"dependencies": "author"}, "author"))
    monkeypatch.setattr(github, "post_comment", lambda repo, pr, body: "created")
    result = run_cli(repo, tmp_path / "out", "--post-to-pr", "7")
    assert result["score"] == 95
    assert "Ignored confidence-ack:dependencies" in (tmp_path / "out" / "report.md").read_text()


def test_acknowledging_one_kind_never_hides_another_with_the_same_name():
    s = Signals(tests_total=5, tests_removed=["t::old"], tests_newly_failing=list("abc"), tests_failed=list("abc"))
    r = score(s, acks={"tests-removed": "@lead"})
    assert [d[0] for d in r.acknowledged] == ["Tests"] and r.acknowledged[0][1] == 15
    assert r.value == 45 and r.band == "Low"  # the failing tests still count
    assert score(s, acks={"tests": "@lead"}).value == 30  # the old display-name label matches nothing


def test_failing_tests_and_missing_results_cant_be_acknowledged():
    failing = Signals(tests_total=5, tests_newly_failing=["a"], tests_failed=["a"])
    assert score(failing, acks={"tests-failing": "@lead"}).value == 55
    assert score(Signals(tests_total=0), acks={"no-tests": "@lead"}).value == 50


def test_report_lists_the_keys_reviewers_can_use():
    r = score(signals())
    md = render(r, signals(), "a" * 40, "b" * 40, None)
    assert "`confidence-ack:metrics`, `confidence-ack:size`" in md
