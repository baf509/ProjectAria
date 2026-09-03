# ARIA persisted desk shells for the Mac control plane.
#
# Source from ~/.zshrc. Interactive coding agents register with ARIA and run
# locally by default. `--corsair`/`--remote` selects an explicitly mapped
# Corsair worktree. `--local` (or `--no-aria`) is the untracked escape hatch:
# it invokes the original tool directly in this terminal — no registration,
# no tmux interception, nothing that an ARIA problem can block or slow down.

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
    local arg remote_mode=0 bypass_aria=0
    local -a forwarded_args
    forwarded_args=()
    for arg in "$@"; do
        case "$arg" in
            --corsair|--remote) remote_mode=1 ;;
            --local|--no-aria) bypass_aria=1 ;;
            *) forwarded_args+=("$arg") ;;
        esac
    done
    if [ "${ARIA_MANAGED:-}" = 1 ]; then
        command "$tool" "${forwarded_args[@]}"
        return $?
    fi
    if (( bypass_aria )); then
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
