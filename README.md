# AI PR Confidence

**How much should you trust a change an AI wrote?** This agent answers with a
0–100 confidence score posted on the pull request, backed by evidence from
tests, coverage, logs and traces, not by another model's opinion.

```
🟡 AI change confidence: 75/100 (Medium)

| Signal | Impact | Evidence                                                    |
| Logs   | −15    | 4 new ERROR log(s) across 1 signature, even though tests pass |
| Traces | −10    | 4 new exception(s) recorded on spans                         |
| ✓      | 0      | All 15 tests pass                                            |
```

## Why this exists

AI coding tools produce changes faster than people can review them, and
"the tests pass" is a weak signal. An AI can write code that passes tests while
swallowing exceptions, add logic no test runs, or quietly delete the test that
was failing. Those problems show up in **runtime telemetry** long before they
show up in a code review. This agent runs the code before and after the change
with OpenTelemetry tracing and log capture turned on, compares the two, and
turns the difference into a score a reviewer can act on.

## How it works

1. Find the merge base of the PR and check out **before** and **after** snapshots (git worktrees).
2. Run the test suite on both with coverage.py and a pytest plugin that installs an
   in-memory OpenTelemetry SDK and captures WARNING+ logs, tagged per test.
3. Compare the runs:

| Signal | Deduction | Why it matters |
|---|---|---|
| Tests that newly fail | −40 first, −5 each more (max −50) | The change broke behavior |
| Tests removed | −15 | AI tools sometimes delete the failing test |
| Changed lines no test executes | up to −30, proportional | Untested logic is unverified logic |
| New ERROR log signatures | −15 first, −5 each more (max −20) | Errors swallowed and logged while tests stay green |
| New exceptions recorded on spans | −10 first, −5 each more (max −15) | Same, seen through traces |
| Traced operations ≥2× slower (and ≥5 ms) | −10 each (max −20) | Performance regressions |
| Very large diff (>400 lines) | −5 | Harder to review |

Bands: **High** ≥ 85, **Medium** 60–84, **Low** < 60.

4. Optionally, a **local model** (Ollama) writes a 2–4 bullet summary of the
   evidence. The model never changes the score, so the number is deterministic
   and every point is explained.

The app being tested only needs the OpenTelemetry **API**. The agent supplies the
SDK at test time, so no changes to the target project are required.

## The demo

`studybuddy/` is a small study-helper app (quiz grading, spaced-repetition
flashcards, study plans) with 15 tests. Four branches simulate AI-generated PRs:

| Branch | What the "AI" did | Expected result |
|---|---|---|
| `feature/study-streaks` | New feature with good tests | 🟢 High |
| `feature/mastery-report` | New feature, no tests | 🟡 Medium: changed lines never run |
| `fix/normalize-quiz-answers` | Tests still pass, but numeric answers now raise, get caught and logged | 🟡 Medium: only telemetry catches it |
| `refactor/planner-rounding` | Breaks a test and deletes another | 🔴 Low |

The third PR is the point of the project: **every test passes**, and a
coverage-only or tests-only gate would approve it.

## Setup on a Mac

```bash
# Python 3.10+ recommended (brew install python@3.12)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Score a branch locally without the model
python -m confidence_agent --base main --head fix/normalize-quiz-answers --source studybuddy --no-llm
cat confidence-report/report.md
```

### Local model on a Windows desktop (free, optional)

1. Install Ollama for Windows from ollama.com, then in a terminal: `ollama pull qwen2.5:3b`
   (any small model works; set `OLLAMA_MODEL` to use another).
2. Let other machines on your home network reach it: Windows Settings → search
   "environment variables" → add a user variable `OLLAMA_HOST` = `0.0.0.0:11434`.
   Quit Ollama from the system tray and start it again.
3. Allow it through the firewall **for private networks only** when Windows asks
   (or add an inbound rule for TCP 11434, Private profile). Don't forward the
   port on your router.
4. Find the desktop's IP with `ipconfig` (IPv4 address, e.g. 192.168.1.50).
5. On the Mac: `export OLLAMA_URL=http://192.168.1.50:11434` and
   test with `curl $OLLAMA_URL/api/tags`.

On an i7-6700 without a GPU, a 3B model takes very roughly tens of seconds per
summary. The agent waits up to 4 minutes and carries on without a summary if
the desktop is off.

### Publish the demo to GitHub (free)

```bash
brew install gh && gh auth login
scripts/publish_demo.sh          # creates the repo, pushes branches, opens 4 PRs
```

The GitHub Action in `.github/workflows/confidence.yml` scores every PR and
posts the result as a single comment that updates on new pushes. GitHub's
servers can't reach your desktop, so CI comments have no summary. To add the
model's summary to a PR's comment, run from your Mac:

```bash
scripts/score_pr.sh 3
```

## Command reference

```
python -m confidence_agent [--repo .] [--base main] [--head HEAD]
                           [--source PKG] [--tests tests] [--out confidence-report]
                           [--no-llm] [--llm-url URL] [--llm-model NAME]
                           [--post-to-pr N] [--min-score N]
```

`--min-score 60` makes the command exit non-zero below 60, so it can gate merges.

## Limitations and next steps

- Python + pytest only for now; the signal and scoring layers are language-agnostic.
- Telemetry comes from test runs, so it only sees code paths tests exercise.
  Next: replay recorded production traffic, or score canary deployments with the same signals.
- Weights are hand-tuned. Next: label a set of real AI-generated PRs
  (merged cleanly vs. reverted or caused incidents) and fit weights to them.
- Latency comparison uses medians from a single run; repeated runs would cut noise.
