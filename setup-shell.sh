#!/bin/sh
set -eu
ATS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ZSHRC="$HOME/.zshrc"
LINE="source \"$ATS_ROOT/shell/ats.zsh\""
touch "$ZSHRC"
if ! grep -Fqx "$LINE" "$ZSHRC"; then
    printf '\n%s\n' "$LINE" >> "$ZSHRC"
fi
printf 'ATS shortcut configured. Run: source "%s"\n' "$ZSHRC"
