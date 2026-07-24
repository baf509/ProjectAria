# ARIA - desk-path model routing for Claude Code
#
# Source this from ~/.bashrc (corsair) or ~/.zshrc (MacBook):
#     source /home/ben/Development/ProjectAria/scripts/aria-claude.sh
#     # MacBook, after syncing the repo or just this file + aria-route-task:
#     source ~/.config/aria/aria-claude.sh
#
# It wraps `claude` so that typing
#     claude "design how we'd split shells out of the API"
# asks ARIA which model the task needs, then starts Claude Code on that model
# inside a `claude-*` tmux session — which both machines already auto-adopt into
# the fleet (tmux hook on corsair, aria-node's capture loop on the MacBook).
#
# Identical on both hosts; the only per-machine difference is which API URL
# resolves, which ~/.config/aria/hosts already handles.
#
# Bare `claude` with no task, `--model`/`--resume`/`--continue`, an
# ARIA-launched session, or an unreachable API all fall through to the real
# binary unchanged — the wrapper never blocks you from working.

: "${ARIA_CLAUDE_ROUTER:=$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")/aria-route-task}"
: "${ARIA_CLAUDE_TIMEOUT:=8}"
# What bare `claude` (no task to classify) should do. Empty = the real binary.
# Set it to the name of a shell function to keep a pre-existing wrapper — on
# corsair it's the per-directory persisted-shell wrapper in ~/.bashrc, which
# this file is sourced *after* so routing wins for the with-a-task case only.
: "${ARIA_CLAUDE_BARE_COMMAND:=}"

_aria_claude_bare() {
    if [ -n "$ARIA_CLAUDE_BARE_COMMAND" ]; then
        "$ARIA_CLAUDE_BARE_COMMAND" "$@"
    else
        command claude "$@"
    fi
}

claude() {
    # ---- bail-outs: hand straight to the real binary -----------------------
    # ARIA_MANAGED is set by the coding backends (agents/backends/claude_code.py).
    # The shell substrate launches the agent under `bash -lc`, which sources this
    # file — without the guard an ARIA-spawned session would call the wrapper,
    # which would call ARIA, which would spawn another session.
    if [ -n "$ARIA_MANAGED" ]; then
        command claude "$@"
        return $?
    fi
    # No task to classify — nothing for the router to do, so hand off to
    # whatever bare `claude` meant before this file was sourced.
    if [ "$#" -eq 0 ]; then
        _aria_claude_bare "$@"
        return $?
    fi
    # Escape hatch inherited from the older corsair wrapper: `claude --no-aria …`
    # skips every wrapper and runs the real binary.
    if [ "$1" = "--no-aria" ]; then
        shift
        command claude "$@"
        return $?
    fi
    # Non-interactive shell: nothing to attach to, and it's almost certainly a
    # script or an `-lc` launch rather than you typing.
    case "$-" in
        *i*) ;;
        *) command claude "$@"; return $? ;;
    esac
    # Anything that already pins a model, resumes, or is a flag-only invocation
    # is not a fresh task to classify.
    case " $* " in
        *" --model "*|*" -m "*|*" --resume "*|*" --continue "*|*" -c "*|*" -p "*|*" --print "*|*" --help "*|*" -h "*)
            command claude "$@"; return $? ;;
    esac
    case "$1" in
        -*) command claude "$@"; return $? ;;
    esac
    if ! command -v tmux >/dev/null 2>&1; then
        command claude "$@"
        return $?
    fi

    # ---- ask ARIA ----------------------------------------------------------
    local _out _rc _model _tier _why
    # stderr is left visible: the helper's failure messages are one line each and
    # tell you whether it was the network, the key, or the API.
    _out=$("$ARIA_CLAUDE_ROUTER" --timeout "$ARIA_CLAUDE_TIMEOUT" -- "$@")
    _rc=$?

    if [ "$_rc" -eq 10 ]; then
        # Light task the judge could answer outright — no session, no tmux.
        printf '%s\n' "$_out"
        return 0
    fi

    if [ "$_rc" -ne 0 ]; then
        # API unreachable / routing disabled / judge failed. Never block.
        printf 'aria: router unavailable — launching on the default model\n' >&2
        command claude --dangerously-skip-permissions "$@"
        return $?
    fi

    _model=$(printf '%s\n' "$_out" | sed -n '1p')
    _tier=$(printf '%s\n' "$_out" | sed -n '2p')
    _why=$(printf '%s\n' "$_out" | sed -n '3p')
    printf 'aria: %s — %s → %s\n' "$_tier" "$_why" "$_model" >&2

    # ---- name the session so both hosts auto-adopt it ----------------------
    # The `claude-` prefix is what the tmux hook (corsair) and the aria-node
    # capture loop (MacBook) match on; see shells_tmux_session_prefix.
    local _slug _name _n
    # Truncation can land mid-word, so strip separators again *after* cutting.
    _slug=$(printf '%s' "$*" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's/[^a-z0-9]\{1,\}/-/g' \
        | cut -c1-28 \
        | sed -e 's/^-\{1,\}//' -e 's/-\{1,\}$//')
    [ -n "$_slug" ] || _slug="task"
    _name="claude-$_slug"
    _n=2
    while tmux has-session -t "=$_name" 2>/dev/null; do
        _name="claude-$_slug-$_n"
        _n=$((_n + 1))
    done

    # ARIA_MANAGED inside the pane keeps a nested `claude` invocation from
    # re-entering the wrapper.
    local _cmd
    _cmd="ARIA_MANAGED=1 command claude --dangerously-skip-permissions --model $(_aria_shquote "$_model") $(_aria_shquote "$*")"
    tmux new-session -d -s "$_name" -c "$PWD" -x 200 -y 50 "$_cmd" || {
        printf 'aria: tmux session create failed — launching inline\n' >&2
        ARIA_MANAGED=1 command claude --dangerously-skip-permissions --model "$_model" "$@"
        return $?
    }

    # SSH'd into corsair you're often already inside tmux; attaching would nest
    # and look broken.
    if [ -n "$TMUX" ]; then
        tmux switch-client -t "$_name"
    else
        tmux attach -t "$_name"
    fi
}

# POSIX-safe single-quote escaping (bash's printf %q and zsh's ${(q)} differ).
_aria_shquote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}
