#!/usr/bin/env bash
#
# coverage-check.sh — the coverage ratchet gate.
#
# Reads the normalized summary (`{"metric": "lines", "percent": <float>}`, produced by
# coverage-adapt.sh) and the repo's `coverage.floor` from .claude/maintainerd.json, and
# fails when the measured percentage is below the floor. Equal passes: the floor is the
# lowest acceptable value, not a value to beat.
#
# Canonical source: maintainerd, plugins/core/scripts/coverage-check.sh
# maintainerd-core: 0.6.41
# A consuming repo carries a copy at .claude/maintainerd/coverage-check.sh so CI can run
# it without the plugin installed. The `maintainerd-core:` line above is the version this
# copy was taken from; the release tooling rewrites it, and `doctor` compares it with the
# loaded plugin (scripts/vendored-stamp-check.sh). Re-copy when it reports a difference.
#
#   coverage-check.sh
#   coverage-check.sh --summary build/coverage-summary.json
#   coverage-check.sh --floor 80        # override the config, for a local what-if
#
# There are no tool-specific threshold flags anywhere in the pipeline: the gate is this
# one shell step in every repo, and the number it reads lives in one tracked file, so
# lowering it is a reviewable diff rather than an invisible edit to a workflow.
#
# The workflow uploads the summary as an artifact named `coverage`; `doctor` reads that
# artifact from the latest default-branch run. See the workflow snippet in the plugin's
# references/config-schema.md.
#
# Requires: bash 3.2+, jq.
#
# Fails closed. A missing summary, malformed JSON, or an absent floor is an error, never
# a pass — every one of those states is indistinguishable from "the tests did not run",
# and a gate that waves those through is a gate that has never once fired.

set -euo pipefail
export LC_ALL=C

usage() {
  cat >&2 <<'USAGE'
usage: coverage-check.sh [--summary FILE] [--config FILE] [--floor N]

  --summary  normalized coverage summary. Default: coverage-summary.json
  --config   repo config holding coverage.floor. Default: .claude/maintainerd.json
  --floor    use this floor instead of the one in the config (an integer, 0-100)
USAGE
}

summary="coverage-summary.json"
config=".claude/maintainerd.json"
floor=""

while [ $# -gt 0 ]; do
  case "$1" in
    --summary) [ $# -ge 2 ] || { echo "coverage-check: --summary needs a value" >&2; exit 2; }; summary="$2"; shift 2 ;;
    --config)  [ $# -ge 2 ] || { echo "coverage-check: --config needs a value" >&2; exit 2; }; config="$2"; shift 2 ;;
    --floor)   [ $# -ge 2 ] || { echo "coverage-check: --floor needs a value" >&2; exit 2; }; floor="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "coverage-check: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

command -v jq >/dev/null 2>&1 || {
  echo "coverage-check: jq is not installed — the summary cannot be read" >&2; exit 2; }

# GitHub Actions renders `::error::` as an annotation on the job. Elsewhere it is just a
# line of text, so this costs nothing off CI.
fail() {
  if [ -n "${GITHUB_ACTIONS:-}" ]; then printf '::error::%s\n' "$1" >&2; else printf '%s\n' "$1" >&2; fi
  shift
  for line in "$@"; do printf '  %s\n' "$line" >&2; done
  exit 1
}

# ── The floor ────────────────────────────────────────────────────────────────
if [ -z "$floor" ]; then
  [ -f "$config" ] || fail "coverage-check: no $config — the floor lives there." \
    "Run /bootstrap in this repo, then /bootstrap --adopt to measure and record the floor."
  jq -e 'type == "object"' "$config" >/dev/null 2>&1 \
    || fail "coverage-check: $config is not a valid JSON object."
  floor="$(jq -r 'if (.coverage | type) == "object" then (.coverage.floor // empty) else empty end' "$config")"
  [ -n "$floor" ] || fail "coverage-check: $config has no coverage.floor — this repo has not adopted the ratchet." \
    "Run /bootstrap --adopt to measure the default branch and record coverage.floor," \
    "or drop this step from the workflow if the repo is exempt from coverage."
fi

case "$floor" in
  ''|*[!0-9]*) fail "coverage-check: coverage.floor is '$floor'; it must be a whole number of percent." ;;
esac
[ "$floor" -le 100 ] || fail "coverage-check: coverage.floor is $floor, which is above 100%."

# ── The measurement ──────────────────────────────────────────────────────────
[ -f "$summary" ] || fail "coverage-check: $summary does not exist." \
  "The repo's commands.coverage must leave a summary there, normalized by coverage-adapt.sh." \
  "Failing closed: a missing summary is indistinguishable from a suite that never ran."

# `type == "object"`, not a bare parse check: indexing an array or a bare string with
# .metric is a jq error, and it would surface as a raw jq message instead of a finding.
jq -e 'type == "object"' "$summary" >/dev/null 2>&1 \
  || fail "coverage-check: $summary is not a JSON object — it may be truncated or the wrong file." \
  "Failing closed rather than reading a percentage out of it."

metric="$(jq -r '.metric // empty' "$summary")"
[ "$metric" = "lines" ] || fail "coverage-check: $summary reports metric '${metric:-<absent>}', expected 'lines'." \
  "Run coverage-adapt.sh over the tool's native output first; the gate reads only the normalized shape."

percent="$(jq -r 'select(.percent | type == "number") | .percent // empty' "$summary")"
[ -n "$percent" ] || fail "coverage-check: $summary has no numeric .percent." \
  "Run coverage-adapt.sh over the tool's native output first."

awk -v p="$percent" 'BEGIN { exit !(p + 0 >= 0 && p + 0 <= 100) }' \
  || fail "coverage-check: $summary reports percent=$percent, which is not in 0-100."

# ── The gate ─────────────────────────────────────────────────────────────────
# awk, not bash arithmetic: the percentage is a float and bash 3.2 has integers only.
# Truncating it here would let a real regression from 85.0 to 84.9 read as no change.
if awk -v p="$percent" -v f="$floor" 'BEGIN { exit !(p + 0 < f + 0) }'; then
  fail "coverage-check: line coverage ${percent}% is below the floor of ${floor}%." \
    "The floor is a ratchet: it may rise, never fall. Add tests for what this change" \
    "left uncovered rather than lowering coverage.floor in $config."
fi

printf 'coverage-check: line coverage %s%% (floor %s%%) — OK\n' "$percent" "$floor"
