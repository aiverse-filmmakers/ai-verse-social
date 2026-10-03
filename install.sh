#!/usr/bin/env bash
set -euo pipefail
repo='aiverse-filmmakers/ai-verse-social'
checkout=''
cleanup() { if [[ -n "$checkout" ]]; then rm -rf -- "$checkout"; fi; }
trap cleanup EXIT
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/dev/null}")" 2>/dev/null && pwd) || base=''
if [[ ! -f "$base/installer/install.py" ]]; then
  checkout=$(mktemp -d)
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh repo clone "$repo" "$checkout/repo" -- --depth 1
  else
    command -v git >/dev/null 2>&1 || { echo 'Please install Git first.' >&2; exit 1; }
    GIT_TERMINAL_PROMPT=0 git clone --depth 1 "https://github.com/$repo.git" "$checkout/repo" || { echo 'Private repository? Sign into GitHub CLI with gh auth login, then use the private install command in the README.' >&2; exit 1; }
  fi
  base="$checkout/repo"
fi
python_bin=''
for candidate in "${AI_VERSE_PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3; do
  [[ -n "$candidate" ]] || continue
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
    python_bin="$candidate"; break
  fi
done
[[ -n "$python_bin" ]] || { echo 'AI-Verse Social needs Python 3.11 or newer. Install Python, then run this command again.' >&2; exit 1; }
"$python_bin" "$base/installer/install.py" "$@"
