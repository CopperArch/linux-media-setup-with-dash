#!/bin/bash
# dashboard-terminal.sh — the shell that backs the dashboard's terminal pane.
#
# Starts Claude Code straight away, but drops to an interactive shell when it
# exits (or if it is missing) instead of killing the session — otherwise
# quitting claude would leave a dead black pane in the dashboard.
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME" || exit 1
echo "── Claude Code — dashboard pane ───────────────────────────────"
echo "   exit claude to get a normal shell; the pane reconnects by itself."
echo
if command -v claude >/dev/null 2>&1; then
    claude
    echo
    echo "── claude exited — dropping to a shell (run 'claude' to restart) ──"
fi
exec bash -il
