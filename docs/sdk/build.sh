#!/usr/bin/env bash
# Assemble each SDK's .github/code-review/REVIEW.md from the shared rules, the
# repository's public-surface definition, and its house rules if it has any.
#
# Six hand-maintained copies of the same rules drift. This is why the shared
# part lives in one file: edit shared.md, run this, and every repository gets
# the same text.
#
# Usage: docs/sdk/build.sh [OUTDIR]   (default: build/sdk)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/../../build/sdk}"

# repo:surface
repos="sdk-py:py sdk-php:php sdk-go:go sdk-java:java sdk-js:js sdk-csharp:csharp"

for pair in $repos; do
  repo="${pair%%:*}"
  lang="${pair##*:}"
  dest="$out/$repo"
  mkdir -p "$dest"
  {
    echo "@include default"
    echo
    cat "$here/shared.md"
    echo
    cat "$here/surface/$lang.md"
    if [ -f "$here/house/$lang.md" ]; then
      echo
      cat "$here/house/$lang.md"
    fi
  } > "$dest/REVIEW.md"
  printf '%-11s %5s lines  %s\n' "$repo" "$(wc -l < "$dest/REVIEW.md")" "$dest/REVIEW.md"
done
