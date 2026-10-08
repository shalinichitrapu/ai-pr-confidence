#!/usr/bin/env bash
# One-time setup: create a GitHub repo from this folder, push every demo branch,
# and open one pull request per branch. The GitHub Action then scores each PR.
#
#   scripts/publish_demo.sh                 # public repo named ai-pr-confidence
#   scripts/publish_demo.sh my-name private # custom name, private repo
set -euo pipefail
NAME="${1:-ai-pr-confidence}"
VISIBILITY="${2:-public}"
cd "$(dirname "$0")/.."

git checkout -q main
gh repo create "$NAME" "--$VISIBILITY" --source . --remote origin --push
git push -q origin --all

open_pr () {
  gh pr create --base main --head "$1" --title "$2" --body "$3" >/dev/null
  echo "opened: $2"
}

open_pr feature/study-streaks \
  "Add study streak tracking" \
  "AI-generated: tracks consecutive study days, with tests."
open_pr feature/mastery-report \
  "Add topic mastery report" \
  "AI-generated: summarizes quiz history into mastery levels per topic."
open_pr fix/normalize-quiz-answers \
  "Normalize quiz answers before grading" \
  "AI-generated: trims whitespace and ignores case so 'Paris ' matches 'paris'."
open_pr refactor/planner-rounding \
  "Make study plan minutes add up exactly" \
  "AI-generated: distributes leftover minutes so each day totals minutes_per_day."

echo
echo "Done. The 'AI change confidence' check will comment on each PR in a minute or two:"
gh pr list
