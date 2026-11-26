#!/usr/bin/env bash
#
# coverage-adapt.sh — normalize a coverage tool's native JSON into the maintainerd
# coverage summary contract:
#
#     {"metric": "lines", "percent": <float>}
#
# Canonical source: maintainerd, plugins/core/scripts/coverage-adapt.sh
# maintainerd-core: 0.6.41
# A consuming repo carries a copy at .claude/maintainerd/coverage-adapt.sh so CI can run
# it without the plugin installed. The `maintainerd-core:` line above is the version this
# copy was taken from; the release tooling rewrites it, and `doctor` compares it with the
# loaded plugin (scripts/vendored-stamp-check.sh). Re-copy when it reports a difference.
#
#   coverage-adapt.sh                                  # auto-detect the one input and its format
#   coverage-adapt.sh --tool pytest-cov                # force the pytest-cov (coverage.py) shape
#   coverage-adapt.sh --tool istanbul --input coverage/coverage-summary.json
#   coverage-adapt.sh --tool llvm-cov --input coverage.json   # llvm-cov export JSON (Swift)
#   coverage-adapt.sh --output build/coverage-summary.json
#
# Line coverage only. Branch coverage is deliberately not gated: it is noisier, moves for
# reasons unrelated to test quality, and a ratchet on it ratchets on noise.
#
# Requires: bash 3.2+, jq.
#
# Exits non-zero — and writes nothing — on anything it cannot read with certainty. A
# coverage gate that guesses a number is worse than one that stops.

set -euo pipefail
# Float parsing and printing must not follow the runner's locale: a comma decimal
# separator turns 87.4 into 87 in awk, and the gate silently moves.
export LC_ALL=C

usage() {
  cat >&2 <<'USAGE'
usage: coverage-adapt.sh [--tool auto|pytest-cov|istanbul|llvm-cov] [--input FILE] [--output FILE]

  --tool    input format. Default "auto": detect from the JSON's shape.
              pytest-cov  coverage.py / pytest-cov `--cov-report=json`  (.totals.percent_covered)
              istanbul    vitest / jest / nyc `json-summary`            (.total.lines.pct)
              llvm-cov    `llvm-cov export` JSON (SwiftPM codecov)      (.data[0].totals.lines.percent)
  --input   the tool's native JSON. Default: whichever ONE of coverage-summary.json,
            coverage.json, coverage/coverage-summary.json holds a measurement. Two or more
            is an error, not a ranking: one of them is a previous run's number. This
            script's own normalized output does not count as a rival measurement.
  --output  where to write the normalized summary. Default: coverage-summary.json
USAGE
}

tool=auto
input=""
output="coverage-summary.json"

while [ $# -gt 0 ]; do
  case "$1" in
    --tool)   [ $# -ge 2 ] || { echo "coverage-adapt: --tool needs a value" >&2; exit 2; }; tool="$2"; shift 2 ;;
    --input)  [ $# -ge 2 ] || { echo "coverage-adapt: --input needs a value" >&2; exit 2; }; input="$2"; shift 2 ;;
    --output) [ $# -ge 2 ] || { echo "coverage-adapt: --output needs a value" >&2; exit 2; }; output="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "coverage-adapt: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$tool" in
  auto|pytest-cov|istanbul|llvm-cov) ;;
  *) echo "coverage-adapt: unknown --tool '$tool' (auto|pytest-cov|istanbul|llvm-cov)" >&2; exit 2 ;;
esac

command -v jq >/dev/null 2>&1 || {
  echo "coverage-adapt: jq is not installed — the summary cannot be read" >&2; exit 2; }

# Discovery refuses to choose between two *measurements* rather than ranking them: a repo
# whose tool writes coverage.json while an unrelated coverage-summary.json is still lying
# around has two plausible inputs, and either ranking silently gates the current commit on
# a previous run's number — the one failure this whole gate exists to prevent.
#
# The adapter's own earlier output is the exception, and not a special case so much as the
# absence of one: an already-normalized file sitting at the output path is this script's
# work, not a rival measurement, so a native input beside it simply wins. That keeps the
# step re-runnable, which matters because CI steps get retried.
if [ -z "$input" ]; then
  found=""
  own_output=""
  count=0
  for candidate in coverage-summary.json coverage.json coverage/coverage-summary.json; do
    [ -f "$candidate" ] || continue
    if [ "$candidate" = "$output" ] && jq -e \
        'type == "object" and .metric == "lines" and (.percent | type == "number")' \
        "$candidate" >/dev/null 2>&1; then
      own_output="$candidate"
      continue
    fi
    found="${found:+$found, }$candidate"
    input="$candidate"
    count=$((count + 1))
  done

  if [ "$count" -gt 1 ]; then
    echo "coverage-adapt: more than one coverage JSON is present ($found)." >&2
    echo "  One of them is probably left over from an earlier run, and gating on the wrong" >&2
    echo "  one would check this commit against a previous commit's number. Pass --input to" >&2
    echo "  name the file this run wrote, or delete the stale one before the coverage step." >&2
    exit 1
  fi
  # Nothing but our own output: the re-run no-op.
  if [ "$count" -eq 0 ] && [ -n "$own_output" ]; then input="$own_output"; fi
fi

if [ -z "$input" ] || [ ! -f "$input" ]; then
  echo "coverage-adapt: no coverage JSON found${input:+ at $input}." >&2
  echo "  Run the repo's commands.coverage first, or pass --input." >&2
  exit 1
fi

# `type == "object"` rather than a bare parse check: every shape below indexes the top
# level, and indexing an array or a string is a jq *error*, which would surface as a raw
# jq message rather than as this one.
jq -e 'type == "object"' "$input" >/dev/null 2>&1 || {
  echo "coverage-adapt: $input is not a valid JSON object — refusing to guess a percentage." >&2
  exit 1; }

# Each supported shape, as the jq path to its line-coverage percentage. The normalized
# shape is included so re-running the adapter on its own output is a no-op rather than an
# error: CI steps get re-run, and a step that only works once is a step that breaks.
percent=""
detected=""
# llvm-cov export wraps its totals in a one-element `data` array. A `count` of 0 means it
# measured no lines at all and reports 0 percent for it; like istanbul's "Unknown", that is
# "nothing was measured", not "0% covered", so it matches no shape and fails closed.
LLVM_COV_FILTER='select((.data | type == "array") and (.data[0].totals.lines.percent | type == "number")
                        and ((.data[0].totals.lines.count // 1) > 0)) | .data[0].totals.lines.percent'
try_shape() {
  local name="$1" filter="$2" value
  value="$(jq -r "$filter // empty" "$input" 2>/dev/null || true)"
  if [ -n "$value" ]; then detected="$name"; percent="$value"; return 0; fi
  return 1
}

case "$tool" in
  pytest-cov) try_shape pytest-cov 'select(.totals.percent_covered | type == "number") | .totals.percent_covered' || true ;;
  istanbul)   try_shape istanbul   'select(.total.lines.pct     | type == "number") | .total.lines.pct'             || true ;;
  llvm-cov)   try_shape llvm-cov   "$LLVM_COV_FILTER" || true ;;
  auto)
    try_shape normalized 'select(.metric == "lines" and (.percent | type == "number")) | .percent' \
      || try_shape pytest-cov 'select(.totals.percent_covered | type == "number") | .totals.percent_covered' \
      || try_shape istanbul   'select(.total.lines.pct | type == "number") | .total.lines.pct' \
      || try_shape llvm-cov   "$LLVM_COV_FILTER" \
      || true
    ;;
esac

if [ -z "$percent" ]; then
  echo "coverage-adapt: $input has no line-coverage percentage in the ${tool} shape." >&2
  echo "  Expected .totals.percent_covered (pytest-cov), .total.lines.pct (istanbul) or" >&2
  echo "  .data[0].totals.lines.percent (llvm-cov), as a number. istanbul writes \"Unknown\"," >&2
  echo "  and llvm-cov a lines.count of 0, when they measured no lines at all." >&2
  exit 1
fi

# 0-100 is the only range a percentage can be in. Anything else means the shape matched
# something that is not a percentage (a count, a ratio) and the gate would be nonsense.
awk -v p="$percent" 'BEGIN { exit !(p + 0 >= 0 && p + 0 <= 100) }' || {
  echo "coverage-adapt: $input yielded percent=$percent, which is not in 0-100." >&2; exit 1; }

outdir="$(dirname "$output")"
[ -d "$outdir" ] || mkdir -p "$outdir"

# The temp file is created beside the output, not in TMPDIR: mv is only atomic within one
# filesystem, and on CI runners TMPDIR is regularly on another. A reader — the gate, the
# artifact upload — must never see a half-written summary.
tmp="$(mktemp "$outdir/.coverage-summary.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# Written through jq so the output is valid JSON with a real number, whatever the input's
# formatting. `-n` plus --argjson keeps the value numeric rather than a quoted string.
jq -n --argjson percent "$percent" '{metric: "lines", percent: $percent}' > "$tmp"
# mktemp creates the file 0600; the summary is uploaded as an artifact and read by the
# gate, so give it the ordinary readable mode a generated file would have.
chmod 644 "$tmp"
mv "$tmp" "$output"
trap - EXIT

echo "coverage-adapt: $input ($detected) -> $output  lines ${percent}%"
