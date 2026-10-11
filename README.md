# AI PR Confidence

**How much should you trust a change an AI wrote?** This agent answers with a
0–100 confidence score posted on the pull request, backed by evidence from
tests, coverage, logs and traces, not by another model's opinion.

```
🟡 AI change confidence: 70/100 (Medium)

| Signal | Impact | Evidence                                                  |
| Logs   | −20    | 2 new ERROR logs, a sign of errors being caught and hidden |
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

Nothing in the scoring is specific to AI: it works on any change, whoever wrote
it. It's aimed at AI-written changes because the problems it looks for are ones
AI tools often produce, and because the number of changes they produce makes an
automatic first check worth having. It doesn't replace code review.

## How it works

1. Find the merge base of the PR and check out **before** and **after** snapshots (git worktrees).
2. Run the test suite on both with coverage.py and a pytest plugin that installs an
   in-memory OpenTelemetry SDK (traces and metrics), captures WARNING+ logs and
   measures each test's peak memory with `tracemalloc`, all tagged per test.
3. Compare the runs:

| Signal | Deduction | Why it matters |
|---|---|---|
| Tests that newly fail | −45 first, −5 each more (max −55), so the score is always Low | The change broke behavior |
| Tests that newly fail, then pass when rerun once | −5 (flaky, or made flaky by this change) | One flaky test shouldn't sink an unrelated change |
| No test results at all | −50 | Nothing was measured, so a clean score would mean nothing |
| Tests removed | −15 (a test whose file was renamed, keeping its name, counts as moved, not removed) | AI tools sometimes delete the failing test |
| Changed lines no test executes | up to −30, proportional | Untested logic is unverified logic |
| Changed lines that tests run but don't check: small edits (flip `<`, swap `+`/`-`, `True`/`False`, change a number) that no covering test notices | up to −15, proportional to the share of edits that went unnoticed | Coverage only proves a line ran, not that a test would catch it being wrong |
| Code hidden from these checks: new `# pragma: no cover`; error logging, tracing or metric instruments removed (net, so moved code doesn't count); or a new broad `except` that drops the error without raising, logging or recording it | −10 per kind (max −20) | Hiding code or errors from coverage and telemetry raises the score without making the code safer |
| New ERROR log signatures | −20 first, −5 each more (max −25), so one is enough to leave High | Errors swallowed and logged while tests stay green |
| New exceptions recorded on spans | −10 first, −5 each more (max −15) | Same, seen through traces |
| Traced operations ≥2× slower (and ≥5 ms) | −10 each (max −20) | Performance regressions |
| App metrics that at least doubled in the same test (and grew by ≥3), or new error-like counters | −10 first, −5 each more (max −20) | The same test doing much more work (e.g. repeated queries), or failures now being counted |
| Security-sensitive code on changed lines: `eval`/`exec`, `pickle`, `yaml.load` without SafeLoader, `os.system`, `shell=True`, `verify=False`, MD5/SHA-1, `tempfile.mktemp`, hard-coded secrets | −10 each (max −20) | Tests and telemetry can't judge these; a small built-in subset of what bandit checks |
| Dependencies added or changed (`requirements*.txt`, `pyproject.toml`) | −5 | A new package is new code nobody here reviewed |
| Tests whose peak memory at least doubled (and grew by ≥5 MB) | −10 each (max −20) | Leaks, unbounded caches, loading too much at once |
| Very large diff (>400 lines, not counting test files) | −5 | Harder to review |

Bands: **High** ≥ 85, **Medium** 60–84, **Low** < 60.

Changed files the tests can't exercise (configuration, data, CI, Dockerfiles) are
listed in the report for direct review. If a change touches only those, the report
warns that the score says little about it.

Logs, exceptions on spans and latency are scored fully only for tests that exist
both before and after the change, because those ran the same inputs. Errors that
appear only in tests the change adds count at half weight: they're the only place a
new feature's swallowed errors can show up. If the new test expects an exception
(`pytest.raises` or `assertRaises`), its errors are listed but not scored.

4. Optionally, a **local model** (Ollama) writes a 2–4 bullet summary of the
   evidence. The model never changes the score, and every point is explained.
   Tests, coverage, logs and metrics give the same result every time for the same
   code. Latency and memory come from a single measured run, so they can vary a
   little between runs.

The app being tested only needs the OpenTelemetry **API**. The agent supplies the
SDK at test time, so no changes to the target project are required.

**How metrics are compared.** Each test runs the same inputs before and after, so
its metrics should barely move. For every metric series (name plus attributes),
the agent compares what one test recorded in each run:

- **Counters and up-down counters:** the amount added during the test.
- **Gauges:** the last value set.
- **Histograms:** the mean of the recorded values.

A counter is "error-like" if its name or an attribute value contains error,
fail, invalid, exception, retry/retries, timeout or reject. A new one of those in a test
that already existed is flagged, even without a before value to compare.

## Install

You need Python 3.9+ and git. The agent runs on macOS and Linux. Windows isn't supported.

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
  Separate several with commas (`--source app,lib`).
  Changed files outside it (e.g. `setup.py` or scripts) aren't checked for coverage.
- `--tests`: your test folder, if it isn't `tests`.

The report is written to `confidence-report/report.md`, with the raw numbers in
`result.json`. The agent checks out both versions in temporary git worktrees,
so your working copy isn't touched.

Your code doesn't need any changes. You always get the test, coverage, log and
memory signals. If your code already uses the OpenTelemetry API, you also get:

- **Tracing:** exceptions on spans and slowdowns.
- **Metrics:** counters, up-down counters, gauges and histograms compared per test.

Peak-memory tracking makes tests run roughly 2–3× slower. Pass `--no-memory` to skip it.

**Installed projects and `src/` layouts.** The agent puts each checkout (and its
`src/` folder, if there is one) first on the import path, so tests import the
version being scored even if the project is installed with `pip install -e`. If
tests still import files from your working copy, the report shows a warning that
the results may be unreliable.

**Parallel tests.** If your pytest config uses pytest-xdist (`-n`), the agent runs
tests in a single process. Coverage and telemetry can't see parallel workers.

## Configuration

Add a `[tool.confidence]` table to your project's `pyproject.toml`. Everything is
optional; command-line flags win over the file.

```toml
[tool.confidence]
source = "my_package"          # default for --source
tests = "tests"                # default for --tests
min_score = 60                 # default for --min-score
ignore = ["*/migrations/*", "*_pb2.py"]               # changed files that are never scored
expected_errors = ["payments.py:refund: TimeoutError"]  # error signatures shown but not scored

[tool.confidence.bands]
high = 85
medium = 60

[tool.confidence.thresholds]
large_diff_lines = 400
latency_ratio = 2.0
latency_min_delta_ms = 5.0
metric_ratio = 2.0
metric_min_delta = 3.0
memory_ratio = 2.0
memory_min_delta_mb = 5.0
max_mutants = 20               # mutation testing budget per run; 0 turns it off
mutant_timeout_s = 60.0

[tool.confidence.weights]      # [first finding, each additional, maximum]
tests_failing = [45, 5, 55]
tests_flaky = [5, 0, 5]
no_tests = [50, 0, 50]
tests_removed = [15, 0, 15]
coverage = [30, 0, 30]         # proportional: the maximum is what 0% coverage costs
error_logs = [20, 5, 25]
span_exceptions = [10, 5, 15]
latency = [10, 10, 20]
visibility = [10, 10, 20]
metrics = [10, 5, 20]
memory = [10, 10, 20]
size = [5, 0, 5]               # [0, 0, 0] turns a check off
dependencies = [5, 0, 5]
security = [10, 10, 20]
mutation = [15, 0, 15]         # proportional: the maximum is what "no edit was noticed" costs
```

The values above are the defaults. An error signature is the text after "New
ERROR logs" or "New exceptions on spans" in a report; `expected_errors` matches
any part of it.

**Settings are read from the base commit, not the pull request.** A pull request
that changes `[tool.confidence]` is scored with the old settings, so it can't
loosen its own checks. Its new settings apply once it's merged. A mistake in the
table (an unknown key or a wrong type) stops the run with a message saying where.

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

      - name: Add report to job summary
        if: always()
        run: cat confidence-report/report.md >> "$GITHUB_STEP_SUMMARY" || true
```

The agent posts a single comment and updates it on each new push. Add
`--min-score 60` to fail the check below 60, so you can make it required before merging.

Pull requests from forks get a read-only token, so GitHub refuses the comment.
The agent prints a warning instead of failing, and the report still appears on
the run's summary page.

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
flashcards, study plans) with 15 tests. It records OpenTelemetry traces and
metrics: counters for graded answers, flashcard reviews and plan allocations,
a gauge for the next review interval, and histograms for quiz scores and minutes
per topic. Five branches simulate AI-generated PRs:

| Branch | What the "AI" did | Expected result |
|---|---|---|
| `feature/study-streaks` | New feature with good tests | 🟢 High |
| `feature/mastery-report` | New feature, no tests | 🟡 Medium: changed lines never run |
| `fix/normalize-quiz-answers` | Tests still pass, but numeric answers now raise, get caught and logged | 🟡 Medium: only telemetry catches it |
| `refactor/planner-rounding` | Breaks a test and deletes another | 🔴 Low |
| `refactor/planner-per-day` | Same plan, but rebuilt for every day: 3× the work in one test | 🟢 High (90): only metrics catch it |

The third branch is the point of the project: **every test passes**, and a
coverage-only or tests-only gate would approve it.

### Try it

Clone this repository to get the demo app and all five branches:

```bash
git clone https://github.com/shalinichitrapu/ai-pr-confidence.git
cd ai-pr-confidence
python3 -m venv .venv && source .venv/bin/activate
pip install .

confidence-agent --base origin/main --head origin/fix/normalize-quiz-answers --source studybuddy --no-llm
cat confidence-report/report.md
```

Swap in any of the other demo branches for `--head`.

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

The summary appears below the score table, marked as the model's reading. The
model sees text the pull request controls (test names, log messages, file names),
so that text is clipped and fenced off as data in the prompt, and the reply is cut
down to at most four plain bullets with links and HTML removed. Treat the summary
as a hint, and the table as the evidence.

### Setting up a local model (free)

The agent uses [Ollama](https://ollama.com). Run it on the same machine as the
agent, or on another computer on your home network (e.g. a desktop with more RAM).
The model machine can run macOS, Linux or Windows, even though the agent itself
needs macOS or Linux.

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
export OLLAMA_URL=http://192.168.1.50:11434
curl $OLLAMA_URL/api/tags
```

Then run the agent without `--no-llm`. Or pass `--llm-url` instead of setting
`OLLAMA_URL`.

**Speed.** On an older CPU with no GPU (e.g. an i7-6700), a 3B model takes very
roughly tens of seconds per summary. A recent Mac or a GPU is much faster.

## Command reference

```
confidence-agent [--repo .] [--base main] [--head HEAD]
                 [--source PKG] [--tests tests] [--out confidence-report]
                 [--no-memory] [--no-llm] [--llm-url URL] [--llm-model NAME]
                 [--post-to-pr N] [--min-score N]
```

`--min-score 60` makes the command exit non-zero below 60, so it can gate merges.

## Developing the agent

The agent's own tests are in `tests/agent/`. They cover how signals are
computed and how they are scored. The demo app's tests are the rest of `tests/`.

```bash
pip install -e .
pytest tests/agent      # the agent
pytest tests            # the agent and the demo app
```

This repository scores its own pull requests. The workflow measures coverage
for both `studybuddy` and `confidence_agent`, so a change to the agent loses
points if its new code isn't tested.

## Limitations and next steps

- Python + pytest only for now; the signal and scoring layers are language-agnostic.
- Telemetry comes from test runs, so it only sees code paths tests exercise.
  Next: replay recorded production traffic, or score canary deployments with the same signals.
- **Weights and thresholds were picked by hand** while building the demo, and
  haven't been checked against real codebases yet. They're constants at the top of
  `confidence_agent/signals.py` and `confidence_agent/scoring.py`. Next: score real
  past PRs (merged cleanly vs. reverted or caused incidents) and fit the weights to
  those outcomes.
- Latency comparison uses medians from a single run; repeated runs would cut noise.
- Metric thresholds (2× and +3) ignore units, and a metric that grows because of a
  wanted feature is still flagged. Read metric deductions as "look here", not "this is a bug".
- `tracemalloc` only sees memory Python allocates; some native libraries' memory isn't counted.
- **Background work:** a span is credited to the test running when it *started*, and a
  child span to its parent's test, so work that outlives its test is attributed
  correctly. A log or metric recorded by a background thread with no trace context
  after its test ended still counts for whichever test is running at that moment.
- **Cost:** the test suite runs twice (before and after), under coverage, and memory
  tracking makes it roughly 2–3× slower again. Mutation testing then runs the tests
  that cover each mutated line, up to `max_mutants` (20) times. Use `--no-memory` and
  `--no-mutation` for large suites.
- **It can be worked around.** A change written to score well, rather than to be
  correct, can avoid the checks: for example by testing only the happy path, or by
  handling errors without logging them. A High score means no warning signs were
  found, not that the change is correct.
