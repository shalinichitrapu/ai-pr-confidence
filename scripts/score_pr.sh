#!/usr/bin/env bash
# Score a pull request on your Mac, with the local model's summary, and post it.
#
#   scripts/score_pr.sh 3
#
# Needs: gh logged in (gh auth login), and OLLAMA_URL pointing at your desktop,
# e.g. export OLLAMA_URL=http://192.168.1.50:11434
set -euo pipefail
PR="${1:?usage: scripts/score_pr.sh <pr-number>}"
cd "$(dirname "$0")/.."

BASE=$(gh pr view "$PR" --json baseRefName --jq .baseRefName)
HEAD_BRANCH=$(gh pr view "$PR" --json headRefName --jq .headRefName)
git fetch -q origin "$BASE" "$HEAD_BRANCH"

python -m confidence_agent \
  --base "origin/$BASE" \
  --head "origin/$HEAD_BRANCH" \
  --source studybuddy \
  --post-to-pr "$PR"
