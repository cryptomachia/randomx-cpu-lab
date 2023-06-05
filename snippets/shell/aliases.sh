# avoid aliasing rm; use -i explicitly instead
# zsh and bash both accept this file
# NB: ll includes dotfiles
# py points at whatever python3 is first on PATH
# gs uses the short branch format
# source from ~/.zshrc or ~/.bashrc
# Note: mkcd creates intermediate directories

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
