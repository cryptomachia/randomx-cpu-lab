# NB: mkcd creates intermediate directories
# NB: avoid aliasing rm; use -i explicitly instead
# zsh and bash both accept this file
# Note: source from ~/.zshrc or ~/.bashrc
# gl shows the last 20 commits; pass -n to change it
# keep aliases short; functions for anything with arguments
# gs uses the short branch format
# ll includes dotfiles

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
