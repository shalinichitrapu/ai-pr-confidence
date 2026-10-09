# AI PR Confidence

**How much should you trust a change an AI wrote?** This agent answers with a
0–100 confidence score posted on the pull request, backed by evidence from
tests, coverage, logs and traces, not by another model's opinion.

```
🟡 AI change confidence: 75/100 (Medium)

| Signal | Impact | Evidence                                                  |
| Logs   | −15    | 2 new ERROR logs, a sign of errors being caught and hidden |
| Traces | −10    | 2 new exceptions recorded on spans                        |
| ✓      | 0      | All 16 tests pass                                         |
| ✓      | 0      | All 11 changed code lines run under tests                 |
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

## Install

You need Python 3.9+ and git. The agent runs on macOS, Linux and Windows.

Install it into the **same virtual environment as the project you want to
score**. The agent runs your tests with that environment's Python, so your
project's own dependencies must be installed there too.

```bash
pip install git+https://github.com/shalinichitrapu/ai-pr-confidence.git
```

This installs the `confidence-agent` command (`python -m confidence_agent` works too).

## Score a change in your project

Your project needs to be a git repository with pytest tests. From its root folder:

```bash
confidence-agent --base main --head my-branch --source my_package --no-llm
```

- `--base`: the branch the change is going into.
- `--head`: the branch (or commit) with the change. Defaults to your current checkout.
- `--source`: the folder whose coverage you care about, usually your package.
- `--tests`: your test folder, if it isn't `tests`.

The report is written to `confidence-report/report.md`, with the raw numbers in
`result.json`. The agent checks out both versions in temporary git worktrees,
so your working copy isn't touched.

Your code doesn't need any changes. If it already uses the OpenTelemetry API
for tracing, the trace signals (exceptions on spans, slowdowns) work too.
Without it, you still get the test, coverage and log signals.

## Add it to your GitHub pull requests

To score every pull request and post the result as a comment, add this file to
your repository as `.github/workflows/confidence.yml`. Change `my_package` and
the install step for your project's dependencies to match your project.

```yaml
name: AI change confidence

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  score:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0 # the agent needs the base branch history

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install -r requirements.txt # your project's dependencies
      - run: pip install git+https://github.com/shalinichitrapu/ai-pr-confidence.git

      - name: Score this pull request
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          confidence-agent \
            --base origin/${{ github.base_ref }} \
            --head ${{ github.event.pull_request.head.sha }} \
            --source my_package \
            --no-llm \
            --post-to-pr ${{ github.event.pull_request.number }}
```

The agent posts a single comment and updates it on each new push. Add
`--min-score 60` to fail the check below 60, so you can make it required before merging.

GitHub's servers can't reach a model on your own network, so CI comments have no
summary. To add one to a pull request's comment, run the agent from your own
machine with your model running. This needs the [GitHub CLI](https://cli.github.com)
logged in (`gh auth login`):

```bash
git fetch origin
confidence-agent --base origin/main --head origin/my-branch --source my_package --post-to-pr 12
```

## Try the demo

`studybuddy/` is a small study-helper app (quiz grading, spaced-repetition
flashcards, study plans) with 15 tests. Four branches simulate AI-generated PRs:

| Branch | What the "AI" did | Expected result |
|---|---|---|
| `feature/study-streaks` | New feature with good tests | 🟢 High |
| `feature/mastery-report` | New feature, no tests | 🟡 Medium: changed lines never run |
| `fix/normalize-quiz-answers` | Tests still pass, but numeric answers now raise, get caught and logged | 🟡 Medium: only telemetry catches it |
| `refactor/planner-rounding` | Breaks a test and deletes another | 🔴 Low |

The third branch is the point of the project: **every test passes**, and a
coverage-only or tests-only gate would approve it.

### Try it

Clone this repository to get the demo app and all four branches:

**macOS / Linux**

```bash
git clone https://github.com/shalinichitrapu/ai-pr-confidence.git
cd ai-pr-confidence
python3 -m venv .venv && source .venv/bin/activate
pip install .

confidence-agent --base origin/main --head origin/fix/normalize-quiz-answers --source studybuddy --no-llm
cat confidence-report/report.md
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/shalinichitrapu/ai-pr-confidence.git
cd ai-pr-confidence
py -3 -m venv .venv; .venv\Scripts\Activate.ps1
pip install .

confidence-agent --base origin/main --head origin/fix/normalize-quiz-answers --source studybuddy --no-llm
Get-Content confidence-report\report.md
```

Swap in any of the other three demo branches for `--head`.

## Optional: plain-English summaries from a local model

### You don't need a local model

The model is optional, and the agent installs and runs fully without it:

- **The score doesn't use it.** Every point comes from tests, coverage, logs and
  traces. The model only turns that evidence into a 2–4 bullet plain-English
  summary, and it is told not to change the score.
- **Nothing to install for it.** The agent's only dependencies are pytest,
  coverage and OpenTelemetry, with no AI libraries. It talks to Ollama over plain
  HTTP using Python's standard library.
- **It fails safely.** Pass `--no-llm` to skip it. Without that flag, if no model
  server answers, the agent prints `LLM summary skipped` and writes the report
  without a summary (it waits at most 4 minutes).
- **CI runs without it.** The GitHub Actions setup above uses `--no-llm`.

Add a model when you want the summary in your reports.

### Setting up a local model (free)

The agent uses [Ollama](https://ollama.com). Run it on the same machine as the
agent, or on another computer on your home network (e.g. a desktop with more RAM).

**1. Install Ollama and pull a model** on the machine that will run the model:

| OS | Install |
|---|---|
| macOS | Download the app from ollama.com, or `brew install ollama` |
| Linux | `curl -fsSL https://ollama.com/install.sh \| sh` (installs a systemd service) |
| Windows | Download the installer from ollama.com |

Then, in a terminal on that machine:

```bash
ollama pull qwen2.5:3b
```

Any small model works. Set `OLLAMA_MODEL` (or pass `--llm-model`) to use another.

**2. Same machine?** You're done. The agent uses `http://localhost:11434` by
default. Check that it works with `curl http://localhost:11434/api/tags`.

**3. Another machine?** By default Ollama only accepts connections from its own
machine. On the model machine, make it listen on the network, then restart Ollama:

| OS | Make Ollama listen on the network |
|---|---|
| macOS | `launchctl setenv OLLAMA_HOST 0.0.0.0:11434`, then quit Ollama from the menu bar and open it again. Re-run the command after a reboot. |
| Linux | `sudo systemctl edit ollama.service`, add the two lines `[Service]` and `Environment="OLLAMA_HOST=0.0.0.0:11434"`, then `sudo systemctl daemon-reload && sudo systemctl restart ollama` |
| Windows | Settings → search "environment variables" → add a user variable `OLLAMA_HOST` = `0.0.0.0:11434`. Quit Ollama from the system tray and start it again. |

Allow port 11434 through the firewall **for your home network only**, and don't
forward the port on your router:

| OS | Firewall |
|---|---|
| macOS | If the firewall is on, click **Allow** when macOS asks about incoming connections for Ollama. |
| Linux | With ufw: `sudo ufw allow from 192.168.1.0/24 to any port 11434 proto tcp` (use your network's range) |
| Windows | Allow it for **private networks only** when Windows asks, or add an inbound rule for TCP 11434 with the Private profile. |

Find the model machine's local IP address (e.g. 192.168.1.50):

| OS | Command |
|---|---|
| macOS | `ipconfig getifaddr en0` |
| Linux | `hostname -I` |
| Windows | `ipconfig` (the IPv4 address) |

**4. Point the agent at it** from the machine that runs the agent:

```bash
# macOS / Linux
export OLLAMA_URL=http://192.168.1.50:11434
curl $OLLAMA_URL/api/tags
```

```powershell
# Windows (PowerShell)
$env:OLLAMA_URL = "http://192.168.1.50:11434"
curl.exe "$env:OLLAMA_URL/api/tags"
```

Then run the agent without `--no-llm`. Or pass `--llm-url` instead of setting
`OLLAMA_URL`.

**Speed.** On an older CPU with no GPU (e.g. an i7-6700), a 3B model takes very
roughly tens of seconds per summary. A recent Mac or a GPU is much faster.

## Command reference

```
confidence-agent [--repo .] [--base main] [--head HEAD]
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
