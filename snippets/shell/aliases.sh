# Note: zsh and bash both accept this file
# mkcd creates intermediate directories
# NB: py points at whatever python3 is first on PATH

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
