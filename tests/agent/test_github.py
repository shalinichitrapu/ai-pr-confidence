"""Posting the PR comment must never fail the run."""

import subprocess

from confidence_agent import github


def test_refused_comment_is_reported_not_raised(monkeypatch, tmp_path):
    def refuse(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="gh: Resource not accessible by integration (HTTP 403)\n")
    monkeypatch.setattr(github.subprocess, "run", refuse)
    outcome = github.post_comment(tmp_path, 7, "body")
    assert outcome.startswith("not posted (gh: Resource not accessible by integration (HTTP 403))")


def test_missing_gh_is_reported_not_raised(monkeypatch, tmp_path):
    def missing(cmd, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(github.subprocess, "run", missing)
    assert github.post_comment(tmp_path, 7, "body").startswith("not posted (gh)")


def test_existing_comment_is_updated(monkeypatch, tmp_path):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        out = "123\n" if "--paginate" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out)
    monkeypatch.setattr(github.subprocess, "run", fake)
    assert github.post_comment(tmp_path, 7, "body") == "updated"
    assert "PATCH" in calls[-1] and calls[-1][-2:] == ["--input", "-"]


def test_new_comment_is_created(monkeypatch, tmp_path):
    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="")
    monkeypatch.setattr(github.subprocess, "run", fake)
    assert github.post_comment(tmp_path, 7, "body") == "created"


def test_only_our_own_comment_is_updated(monkeypatch, tmp_path):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["api", "user"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="")  # the Actions token can't read /user
        if "--paginate" in cmd:
            jq = cmd[cmd.index("--jq") + 1]
            assert '.user.login == "github-actions[bot]"' in jq
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    def run(cmd, **kw):
        result = fake(cmd, **kw)
        if result.returncode and kw.get("check"):
            raise subprocess.CalledProcessError(result.returncode, cmd)
        return result
    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.post_comment(tmp_path, 7, "body") == "created"
