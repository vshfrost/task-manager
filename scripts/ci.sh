#!/usr/bin/env bash
# CI: той самий прогін локально і в GitHub Actions (.github/workflows/ci.yml).
# Без секретів і без живих сервісів: тести працюють на тимчасовому planner (tests/_support.py).
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

# Трековані git-файли за розширенням ($1, regex) або shebang ($2, regex).
tracked_by() {
  git ls-files -z | while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue
    if [[ "$f" =~ $1 ]] || head -n1 "$f" 2>/dev/null | grep -Eq "$2"; then printf '%s\n' "$f"; fi
  done
}

step "shell: bash -n + shellcheck"
sh_files=$(tracked_by '\.sh$' '^#!.*[/ ](bash|sh)( |$)')
if [ -n "$sh_files" ]; then
  printf '%s\n' "$sh_files" | tr '\n' '\0' | xargs -0 -n1 bash -n
  if command -v shellcheck >/dev/null; then
    printf '%s\n' "$sh_files" | tr '\n' '\0' | xargs -0 shellcheck
  elif [ -n "${CI:-}" ]; then
    echo "shellcheck is required in CI" >&2; exit 1
  else
    echo "WARN: shellcheck not installed locally (brew install shellcheck), skipped"
  fi
fi

step "uv sync (frozen lockfile)"
uv sync --frozen

step "python: syntax"
tracked_by '\.py$' '^#!.*python' | tr '\n' '\0' \
  | xargs -0 uv run python -c 'import ast, sys; [ast.parse(open(f, "rb").read(), f) for f in sys.argv[1:]]'

step "tests + coverage (branch, fail_under з pyproject.toml)"
uv run coverage run -m unittest discover -s tests
uv run coverage report

step "CI OK"
