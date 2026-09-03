#!/bin/bash
# dashboard-pane.sh — decides what runs in the dashboard's terminal pane.
#
# ttyd is started with --url-arg, so the page picks a program by URL:
#     http://127.0.0.1:7682/?arg=claude
#     http://127.0.0.1:7682/?arg=htop
#     http://127.0.0.1:7682/?arg=ask&arg=why+is+radarr+failing
#
# Arguments arrive as argv (never a shell string) and the first one is matched
# against a fixed whitelist here, so the URL can only ever select one of these
# programs — it can't smuggle in a command.
#
# Every pane falls back to an interactive shell when its program exits, so the
# pane is never a dead black rectangle.
set -uo pipefail
# ttyd execs this script directly (not as a login/interactive shell), so
# ~/.bashrc never runs — opencode's own PATH entry lives there and has to be
# repeated here or every "command not found: opencode" pane fails silently.
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"
cd "$HOME" || exit 1

PROG="${1:-claude}"
shift || true

# Which local model the llm/askllm panes use. Override in the environment to
# switch without editing this script.
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

# Online DeepSeek — always kept as one of the pane options. Put an API key
# (OpenRouter or compatible) in ~/.config/status-dashboard/deepseek.env:
#   DEEPSEEK_API_KEY=<your-openrouter-key>
#   DEEPSEEK_BASE_URL=https://openrouter.ai/api/v1   (default)
# DEEPSEEK_MODEL/OXALPHA_MODEL/MINIMAX_MODEL/CHATGPT_MODEL below are kept
# current by ai-panes-check.py's managed block in that same file (run
# nightly from daily-routine.sh) — it swaps in a cheap-paid variant if a
# model's free tier disappears rather than dropping the pane, so any of
# these four can end up briefly non-free between runs; see its output for
# the current pricing.
DS_ENV="$HOME/.config/status-dashboard/deepseek.env"
[ -f "$DS_ENV" ] && . "$DS_ENV"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash:free}"

# Ox Alpha (stealth model on OpenRouter — was free during its preview,
# de-anonymized 2026-08-23 as Z.AI's GLM-5.3-Flash; see KNOWN_RENAMES in
# ai-panes-check.py). Same OpenRouter account as DeepSeek above, so it
# reuses DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL — only the model slug differs.
OXALPHA_MODEL="${OXALPHA_MODEL:-stealth/ox-alpha}"

# Minimax M3 (free tier on OpenRouter, top-5 by usage as of 2026-09). Same
# OpenRouter account/key as the two panes above — only the model slug differs.
MINIMAX_MODEL="${MINIMAX_MODEL:-minimax/minimax-m3:free}"

# Best available paid models, one per remaining major provider not already
# covered above. No free tier exists for any of these three on OpenRouter —
# every query bills the OpenRouter account. All three are kept current by
# ai-panes-check.py, run nightly from daily-routine.sh.
CHATGPT_MODEL="${CHATGPT_MODEL:-openai/gpt-5.6-sol-pro}"   # ~$2 / $10 per M tokens
GEMINI_MODEL="${GEMINI_MODEL:-google/gemini-3.8-flash}"     # ~$0.75 / $3.75 per M tokens
HY4_MODEL="${HY4_MODEL:-tencent/hy4-preview}"                # ~$0.83 / $2.50 per M tokens

hr() { printf '── %s %s\n' "$1" "$(printf '─%.0s' $(seq 1 $((60 - ${#1}))))"; }
fallback() {
    echo
    hr "$1 exited — dropping to a shell"
    exec bash -il
}

# Shared helpers for the ask*/agent model panes below — every one-shot pane
# used to repeat this same curl+python block inline.
need_key() {   # need_key <what-the-key-unlocks>; false (after a message) if unset
    if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
        echo "no API key — create ~/.config/status-dashboard/deepseek.env with:"
        echo "  DEEPSEEK_API_KEY=<your key>"
        echo "Key from openrouter.ai ($1)"
        return 1
    fi
}
ask_curl() {   # ask_curl <model> <question> — one-shot OpenRouter chat call
    curl -s -m 120 "$DEEPSEEK_BASE_URL/chat/completions" \
        -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$2")}]}" \
    | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
    print(d["choices"][0]["message"]["content"])
except Exception as e:
    print("request failed:", e)
'
}
press_enter() {
    echo
    hr "done — press enter for a shell"
    read -r _ 2>/dev/null
    exec bash -il
}

case "$PROG" in
    claude)
        hr "Claude Code"
        if command -v claude >/dev/null 2>&1; then claude; else echo "claude not installed"; fi
        fallback "claude" ;;

    opencode)
        hr "opencode"
        if command -v opencode >/dev/null 2>&1; then opencode; else echo "opencode not installed"; fi
        fallback "opencode" ;;

    ask)
        # One-shot LLM query typed into the dashboard's search box. "$*" is a
        # single argv element for claude -p, so spaces and quotes are safe.
        QUERY="$*"
        hr "Ask Claude"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif command -v claude >/dev/null 2>&1; then
            echo "> $QUERY"; echo
            claude -p "$QUERY"
        else
            echo "claude not installed"
        fi
        press_enter ;;

    llm)
        # Interactive chat with the local model. OLLAMA_HOST must be set: the
        # server is a *user* service on loopback, not the old system one.
        hr "Local LLM — $OLLAMA_MODEL"
        export OLLAMA_HOST=127.0.0.1:11434
        if ! curl -sf -m 5 -o /dev/null "http://$OLLAMA_HOST/api/version"; then
            echo "ollama is not running — start it with:"
            echo "  systemctl --user start ollama.service"
        elif ! ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
            echo "model $OLLAMA_MODEL not pulled yet — fetching it now"
            ollama pull "$OLLAMA_MODEL" && ollama run "$OLLAMA_MODEL"
        else
            ollama run "$OLLAMA_MODEL"
        fi
        fallback "ollama" ;;

    askllm)
        # One-shot query against the local model, from the dashboard's box.
        QUERY="$*"
        export OLLAMA_HOST=127.0.0.1:11434
        hr "Ask $OLLAMA_MODEL"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif ! curl -sf -m 5 -o /dev/null "http://$OLLAMA_HOST/api/version"; then
            echo "ollama is not running (systemctl --user start ollama.service)"
        else
            echo "> $QUERY"; echo
            ollama run "$OLLAMA_MODEL" "$QUERY"
        fi
        press_enter ;;

    askds)
        # One-shot query against DeepSeek (near-free — see ai-panes-check.py).
        QUERY="$*"
        hr "Ask DeepSeek — $DEEPSEEK_MODEL (near-free, verify at openrouter.ai)"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif need_key "model $DEEPSEEK_MODEL"; then
            echo "> $QUERY"; echo
            ask_curl "$DEEPSEEK_MODEL" "$QUERY"
        fi
        press_enter ;;

    askoa)
        # One-shot query against Ox Alpha (near-free — see ai-panes-check.py).
        QUERY="$*"
        hr "Ask Ox Alpha — $OXALPHA_MODEL (near-free, verify at openrouter.ai)"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif need_key "this pane uses model $OXALPHA_MODEL"; then
            echo "> $QUERY"; echo
            ask_curl "$OXALPHA_MODEL" "$QUERY"
        fi
        press_enter ;;

    oa)
        # Agentic session with Ox Alpha (near-free — see ai-panes-check.py),
        # run through opencode so it gets real file search/read/write/edit
        # and shell tools (with opencode's own per-action approval prompts —
        # not --auto, so it still asks before writing or running anything).
        hr "Ox Alpha — $OXALPHA_MODEL (near-free, verify at openrouter.ai) — file/shell access via opencode"
        if need_key "this pane uses model $OXALPHA_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$OXALPHA_MODEL"
        fi
        fallback "oxalpha" ;;

    askmm)
        # One-shot query against Minimax M3 (free, online, OpenRouter).
        QUERY="$*"
        hr "Ask Minimax M3 (free)"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif need_key "this pane uses model $MINIMAX_MODEL"; then
            echo "> $QUERY"; echo
            ask_curl "$MINIMAX_MODEL" "$QUERY"
        fi
        press_enter ;;

    mm)
        # Agentic session with Minimax M3 (free, online, OpenRouter), run
        # through opencode for real file/shell tools with its own approval
        # prompts (not --auto).
        hr "Minimax M3 (free) — file/shell access via opencode"
        if need_key "this pane uses model $MINIMAX_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$MINIMAX_MODEL"
        fi
        fallback "minimax" ;;

    askgpt)
        # One-shot query against the best available OpenAI model (PAID).
        QUERY="$*"
        hr "Ask ChatGPT ($CHATGPT_MODEL) — PAID, bills OpenRouter"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif need_key "this pane is PAID, model $CHATGPT_MODEL"; then
            echo "> $QUERY"; echo
            ask_curl "$CHATGPT_MODEL" "$QUERY"
        fi
        press_enter ;;

    gpt)
        # Agentic session with the best available OpenAI model (PAID), run
        # through opencode for real file/shell tools with its own approval
        # prompts (not --auto).
        hr "ChatGPT — $CHATGPT_MODEL (PAID, bills OpenRouter) — file/shell access via opencode"
        if need_key "this pane is PAID, model $CHATGPT_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$CHATGPT_MODEL"
        fi
        fallback "chatgpt" ;;

    askgm)
        # One-shot query against the best available Google model (PAID).
        QUERY="$*"
        hr "Ask Gemini ($GEMINI_MODEL) — PAID, bills OpenRouter"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif need_key "this pane is PAID, model $GEMINI_MODEL"; then
            echo "> $QUERY"; echo
            ask_curl "$GEMINI_MODEL" "$QUERY"
        fi
        press_enter ;;

    gm)
        # Agentic session with the best available Google model (PAID), run
        # through opencode for real file/shell tools with its own approval
        # prompts (not --auto).
        hr "Gemini — $GEMINI_MODEL (PAID, bills OpenRouter) — file/shell access via opencode"
        if need_key "this pane is PAID, model $GEMINI_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$GEMINI_MODEL"
        fi
        fallback "gemini" ;;

    askhy)
        # One-shot query against the best available Tencent model (PAID).
        QUERY="$*"
        hr "Ask Hy4 ($HY4_MODEL) — PAID, bills OpenRouter"
        if [[ -z "${QUERY// }" ]]; then
            echo "no question given"
        elif need_key "this pane is PAID, model $HY4_MODEL"; then
            echo "> $QUERY"; echo
            ask_curl "$HY4_MODEL" "$QUERY"
        fi
        press_enter ;;

    hy)
        # Agentic session with the best available Tencent model (PAID), run
        # through opencode for real file/shell tools with its own approval
        # prompts (not --auto).
        hr "Hy4 — $HY4_MODEL (PAID, bills OpenRouter) — file/shell access via opencode"
        if need_key "this pane is PAID, model $HY4_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$HY4_MODEL"
        fi
        fallback "hy4" ;;

    ds)
        # Agentic session with DeepSeek (near-free — see ai-panes-check.py),
        # run through opencode for real file/shell tools with its own
        # approval prompts (not --auto).
        hr "DeepSeek — $DEEPSEEK_MODEL (near-free, verify at openrouter.ai) — file/shell access via opencode"
        if need_key "model $DEEPSEEK_MODEL"; then
            OPENROUTER_API_KEY="$DEEPSEEK_API_KEY" opencode --model "openrouter/$DEEPSEEK_MODEL"
        fi
        fallback "deepseek" ;;

    shell)
        hr "Shell"; exec bash -il ;;

    htop)
        hr "Processes"
        if command -v htop >/dev/null 2>&1; then htop; else top; fi
        fallback "htop" ;;

    docker)
        hr "Container stats (Ctrl-C to exit)"
        docker stats
        fallback "docker stats" ;;

    logs)
        hr "System log (Ctrl-C to exit)"
        journalctl -f -n 80
        fallback "journalctl" ;;

    dashlog)
        hr "Dashboard collector + repair log"
        journalctl --user -u status-dashboard-server -u status-collect -f -n 60
        fallback "journalctl" ;;

    disk)
        hr "Disk usage — {{MEDIA_POOL}}"
        if command -v ncdu >/dev/null 2>&1; then ncdu {{MEDIA_POOL}}; else du -h -d2 {{MEDIA_POOL}} | sort -h; fi
        fallback "ncdu" ;;

    routine)
        hr "Latest daily-routine log"
        L=$(ls -t "$HOME"/.hermes/maintenance-logs/daily-routine-*.log 2>/dev/null | head -1)
        [[ -n "$L" ]] && less +G "$L" || echo "no daily-routine logs yet"
        fallback "log viewer" ;;

    *)
        hr "Unknown pane '$PROG' — shell"
        exec bash -il ;;
esac
