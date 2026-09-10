# ARIA persisted desk shells for the Mac control plane.
#
# Source from ~/.zshrc. Interactive coding agents register with ARIA and run
# locally by default. `--corsair`/`--remote` selects an explicitly mapped
# Corsair worktree. `--no-aria` bypasses shell tracking; `--local` is its
# Mac-local alias. Plain commands retain the managed default.

_aria_corsair_path() {
    local here="$PWD" local_root remote_root
    while IFS='|' read -r local_root remote_root; do
        if [ "$here" = "$local_root" ]; then
            printf '%s\n' "$remote_root"
            return 0
        fi
        case "$here" in
            "$local_root"/*)
                printf '%s%s\n' "$remote_root" "${here#$local_root}"
                return 0
                ;;
        esac
    done <<'EOF'
/Users/ben/Development/AIProjects|/home/ben/Development/AiProjects
/Users/ben/Development/Emulation|/home/ben/Development/Emulation
/Users/ben/Development/GameDevelopment/theVeilWar|/home/ben/Development/Games/theVeilWar
/Users/ben/Development/GameDevelopment/theVeilWarDuel|/home/ben/Development/Games/theVeilWarDuel
/Users/ben/Development/GameDevelopment/war-audio-game|/home/ben/Development/war-audio-game
/Users/ben/Development/GameDevelopment/AudioTools/audio.cpp-webui|/home/ben/Development/audio.cpp-webui
/Users/ben/Development/AgentWorkspaces/aria-projects|/home/ben/Development/aria-projects
/Users/ben/Development/DataEngineering/MongoDBWorkStuff|/home/ben/Development/MongoDBWorkStuff
EOF
    return 1
}

_aria_shell_quote() {
    printf "'%s'" "${1//\'/\'\\\'\'}"
}

_aria_corsair_command() {
    local command_string='' arg
    for arg in "$@"; do
        command_string+="$(_aria_shell_quote "$arg") "
    done
    ssh -tt -p 2222 \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=yes \
        -o HostKeyAlias=corsair-ai.local \
        -o UserKnownHostsFile=/Users/ben/.config/devbox-migration/corsair-known-hosts-2222 \
        ben@100.123.245.84 "$command_string"
}

_aria_coding_agent() {
    local tool="$1"
    shift
    local arg remote_mode=0 untracked_mode=${ARIA_UNTRACKED:-0} local_mode=0 attach_mode=0
    local -a forwarded_args
    forwarded_args=()
    for arg in "$@"; do
        case "$arg" in
            --corsair|--remote) remote_mode=1 ;;
            --local) untracked_mode=1; local_mode=1 ;;
            --no-aria) untracked_mode=1 ;;
            --aria-view|--aria-takeover)
                attach_mode=1
                if [ "${ARIA_MANAGED:-}" = 1 ]; then
                    echo "aria: attach controls require an SSH prompt outside the managed shell" >&2
                    return 2
                fi
                forwarded_args+=("$arg") ;;
            *) forwarded_args+=("$arg") ;;
        esac
    done
    if (( untracked_mode )); then
        if (( attach_mode || (local_mode && remote_mode) )); then
            echo 'aria: untracked mode cannot attach to a managed shell or combine --local with --corsair' >&2
            return 2
        fi
        if [ -n "${TMUX:-}" ] || [ "${ARIA_MANAGED:-}" = 1 ]; then
            echo 'aria: open a normal terminal outside the managed/tmux shell for an untracked session' >&2
            return 2
        fi
        if (( ! remote_mode )); then
            "$HOME/.local/bin/aria-untracked-shell" "$tool" "${forwarded_args[@]}"
            return $?
        fi
        forwarded_args=(--no-aria "${forwarded_args[@]}")
    fi
    if [ "${ARIA_MANAGED:-}" = 1 ]; then
        command "$tool" "${forwarded_args[@]}"
        return $?
    fi
    if (( ! remote_mode )); then
        "$HOME/.config/aria/aria-local-shell" "$tool" "${forwarded_args[@]}"
        return $?
    fi
    local remote_dir
    if ! remote_dir=$(_aria_corsair_path); then
        printf 'aria: %s is not mapped to Corsair; cd to a mapped project or omit `--corsair`\n' "$PWD" >&2
        return 2
    fi
    _aria_corsair_command /home/ben/.local/bin/aria-remote-shell "$remote_dir" "$tool" "${forwarded_args[@]}"
}

claude() { _aria_coding_agent claude "$@"; }
codex()  { _aria_coding_agent codex "$@"; }
pi()     { _aria_coding_agent pi "$@"; }
