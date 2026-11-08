#!/bin/bash
# Status line script: displays real-time project context
# Configure in settings.json: "statusLine": {"type": "command", "command": ".claude/status_lines/context_status.sh"}

# Git info
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "no-git")
DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
LAST_COMMIT=$(git log -1 --format='%s' 2>/dev/null | head -c 40)

# Project type detection
if [ -f "package.json" ]; then
    PROJECT_TYPE="node"
    PROJECT_NAME=$(python3 -c "import json; print(json.load(open('package.json')).get('name','unknown'))" 2>/dev/null || echo "unknown")
elif [ -f "pyproject.toml" ]; then
    PROJECT_TYPE="python"
    PROJECT_NAME=$(grep '^name' pyproject.toml 2>/dev/null | head -1 | cut -d'"' -f2 || echo "unknown")
elif [ -f "Cargo.toml" ]; then
    PROJECT_TYPE="rust"
    PROJECT_NAME=$(grep '^name' Cargo.toml 2>/dev/null | head -1 | cut -d'"' -f2 || echo "unknown")
else
    PROJECT_TYPE="unknown"
    PROJECT_NAME=$(basename "$(pwd)")
fi

# File counts
SRC_FILES=$(find . -name '*.ts' -o -name '*.tsx' -o -name '*.py' -o -name '*.rs' -o -name '*.go' -o -name '*.js' -o -name '*.jsx' 2>/dev/null | grep -v node_modules | grep -v .git | wc -l | tr -d ' ')
TEST_FILES=$(find . -name '*test*' -o -name '*spec*' 2>/dev/null | grep -v node_modules | grep -v .git | wc -l | tr -d ' ')

# Activity log count (if exists)
ACTIVITY_COUNT=0
if [ -f "logs/activity.jsonl" ]; then
    ACTIVITY_COUNT=$(wc -l < logs/activity.jsonl | tr -d ' ')
fi

# Output JSON for status line
echo "{\"$PROJECT_NAME [$PROJECT_TYPE]\": \"$BRANCH | $DIRTY dirty | $SRC_FILES src / $TEST_FILES test | $ACTIVITY_COUNT actions logged\"}"
