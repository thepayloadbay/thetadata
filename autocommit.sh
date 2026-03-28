#!/bin/bash
# autocommit.sh — commits and pushes any changes to tracked source files.
# Run on a schedule (e.g. hourly via cron). No-ops if nothing changed.

PROJ="/Users/jeerapongwongchote/Documents/thetadata"
cd "$PROJ" || exit 1

# Stage all tracked changes + any new .py, .sh, .md files (data/logs excluded by .gitignore)
git add -u
git add *.py *.sh *.md 2>/dev/null || true

# Check if there's anything staged
if git diff --cached --quiet; then
    exit 0  # nothing to commit
fi

MSG="auto: $(date '+%Y-%m-%d %H:%M') — $(git diff --cached --name-only | tr '\n' ' ')"
git commit -m "$MSG"
git push origin main >> "$PROJ/logs/autocommit.log" 2>&1
echo "[$(date)] Committed and pushed: $MSG" >> "$PROJ/logs/autocommit.log"
