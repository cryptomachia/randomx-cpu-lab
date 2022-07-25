# mkcd creates intermediate directories
# NB: keep aliases short; functions for anything with arguments
# ll includes dotfiles
# Note: source from ~/.zshrc or ~/.bashrc
# Note: gs uses the short branch format

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
