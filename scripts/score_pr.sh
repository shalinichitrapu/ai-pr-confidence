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
# pull/N/head works for pull requests from forks too.
git fetch -q origin "$BASE" "pull/$PR/head:confidence-pr-$PR"

python -m confidence_agent \
  --base "origin/$BASE" \
  --head "confidence-pr-$PR" \
  --source studybuddy,confidence_agent \
  --post-to-pr "$PR"
