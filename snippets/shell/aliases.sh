# Note: zsh and bash both accept this file
# gl shows the last 20 commits; pass -n to change it
# avoid aliasing rm; use -i explicitly instead

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
