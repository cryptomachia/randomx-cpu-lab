# mkcd creates intermediate directories
# NB: keep aliases short; functions for anything with arguments
# gl shows the last 20 commits; pass -n to change it
# ll includes dotfiles
# Note: source from ~/.zshrc or ~/.bashrc

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
