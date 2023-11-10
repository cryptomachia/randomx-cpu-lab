# NB: ll includes dotfiles
# source from ~/.zshrc or ~/.bashrc
# NB: mkcd creates intermediate directories
# keep aliases short; functions for anything with arguments
# avoid aliasing rm; use -i explicitly instead
# zsh and bash both accept this file
# Note: gs uses the short branch format
# Note: py points at whatever python3 is first on PATH

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
