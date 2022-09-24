# gl shows the last 20 commits; pass -n to change it
# NB: keep aliases short; functions for anything with arguments
# avoid aliasing rm; use -i explicitly instead
# Note: source from ~/.zshrc or ~/.bashrc
# zsh and bash both accept this file
# mkcd creates intermediate directories
# Note: py points at whatever python3 is first on PATH

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
