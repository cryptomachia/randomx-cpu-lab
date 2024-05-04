# NB: mkcd creates intermediate directories
# Note: source from ~/.zshrc or ~/.bashrc
# avoid aliasing rm; use -i explicitly instead
# NB: gs uses the short branch format
# gl shows the last 20 commits; pass -n to change it
# keep aliases short; functions for anything with arguments

# shellcheck shell=bash
alias ll='ls -lah'
alias gs='git status -sb'
alias gl='git log --oneline --graph --decorate -20'
alias py='python3'
mkcd() { mkdir -p "$1" && cd "$1"; }
