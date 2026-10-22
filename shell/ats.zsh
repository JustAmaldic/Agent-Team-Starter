# Source this file from ~/.zshrc to enable .ats inside Git repositories.
.ats() {
    local repo_root
    repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
        printf '%s\n' 'ATS: no Git repository found' >&2
        return 1
    }
    if [[ ! -f "$repo_root/.ats/ats.py" ]]; then
        printf '%s\n' 'ATS: .ats/ats.py not found in this repository' >&2
        return 1
    fi
    python3 "$repo_root/.ats/ats.py" "$@"
}
