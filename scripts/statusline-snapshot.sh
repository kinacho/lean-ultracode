#!/usr/bin/env bash
# lean-ultracode: saves the 5-hour / weekly usage the status line receives, so usage.py can project
# a workflow's impact on the usage limits.
#
#   echo "$input" | bash <skill dir>/scripts/statusline-snapshot.sh
#
# Call it from your status line command with the JSON Claude Code passed it on stdin. It prints
# nothing and never fails the status line. It writes:
#   ~/.claude/usage-snapshot.json   the latest snapshot (only when the input carries rate_limits)
#   ~/.claude/usage-history.jsonl   one line {ts, f, fr, w, wr} whenever a % or a reset changes
# Requires jq. Exit code is always 0.
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
snap="$HOME/.claude/usage-snapshot.json"
hist="$HOME/.claude/usage-history.jsonl"
mkdir -p "$HOME/.claude" 2>/dev/null || exit 0
echo "$input" | jq -ce 'select(.rate_limits != null) | {ts: (now|floor), session_id, model: .model.id, rate_limits, cost}' \
  >"$snap.tmp" 2>/dev/null && mv -f "$snap.tmp" "$snap" || rm -f "$snap.tmp"
if [ -s "$snap" ]; then
  cur=$(jq -c '{ts, f: .rate_limits.five_hour.used_percentage, fr: .rate_limits.five_hour.resets_at, w: .rate_limits.seven_day.used_percentage, wr: .rate_limits.seven_day.resets_at}' "$snap" 2>/dev/null)
  last=$(tail -n1 "$hist" 2>/dev/null)
  [ -n "$cur" ] && [ "$(jq -c 'del(.ts)' <<<"$cur")" != "$(jq -c 'del(.ts)' <<<"${last:-{\}}" 2>/dev/null)" ] && echo "$cur" >>"$hist"
fi
exit 0
