#compdef aegis aegis-backup aegis-calendar aegis-contacts aegis-cookbook aegis-docs aegis-gallery aegis-mail aegis-mcp aegis-memory aegis-notes aegis-personal aegis-preset aegis-research aegis-sessions aegis-signature aegis-skills aegis-tasks aegis-theme aegis-webhook
# Zsh tab-completion for the aegis umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/aegis-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `aegis <tab>` completes subcommands; `aegis mail <tab>`
# completes mail subcommands; `aegis-mail <tab>` works the same.

_aegis_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _aegis_subs

_aegis_refresh() {
    _aegis_subs=()
    local dir="$(_aegis_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/aegis-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#aegis-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _aegis_subs[$sub]="$commands"
    done
}

_aegis() {
    [[ ${#_aegis_subs} -eq 0 ]] && _aegis_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "aegis" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_aegis_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_aegis_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_aegis_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # aegis-foo <tab>
    local sub="${cmd#aegis-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_aegis_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_aegis "$@"
