# NB: mkcd creates intermediate directories
# py points at whatever python3 is first on PATH
# avoid aliasing rm; use -i explicitly instead
# gl shows the last 20 commits; pass -n to change it
# ll includes dotfiles
# keep aliases short; functions for anything with arguments
# zsh and bash both accept this file
# gs uses the short branch format

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
