#!/usr/bin/env bash
# Seed a Gatekeeper playground repository with the synthetic demo pack.
#
# You run this yourself; it uses only your existing git (and optionally gh) credentials
# and never reads .env. It clones into a temporary folder, shows a plan and asks you to
# type "yes". It pushes demo/base to main ONLY if the repository has no commits, creates
# one branch per demo PR (skipping existing branches), and prints compare URLs.
# It never deletes branches and never force-pushes.
#
# Usage: bash demo/seed.sh your-name/gatekeeper-playground
set -euo pipefail

repo="${1:-}"
if [[ ! "$repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]]; then
  echo "Usage: bash demo/seed.sh owner/name" >&2
  exit 2
fi
command -v git >/dev/null || { echo "git is required." >&2; exit 2; }

demo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/gatekeeper-seed-XXXXXX")"

echo "Cloning https://github.com/$repo.git into $work"
git clone "https://github.com/$repo.git" "$work"
cd "$work"

empty=0
git rev-parse --verify HEAD >/dev/null 2>&1 || empty=1

echo
echo "Plan for $repo:"
if [[ $empty -eq 1 ]]; then
  echo "  - push demo/base as the first commit on main"
else
  echo "  - the repository already has commits: main will NOT be changed"
fi
for pr in "$demo"/prs/*/; do
  echo "  - create branch '$(tr -d '\r\n' < "$pr/branch.txt")' ($(tr -d '\r\n' < "$pr/title.txt")), unless it exists"
done
echo "  - never delete branches, never force-push"
read -r -p "Type yes to continue: " answer
if [[ "$answer" != "yes" ]]; then echo "Nothing was changed."; exit 1; fi

if [[ $empty -eq 1 ]]; then
  cp -R "$demo/base/." "$work/"
  git checkout -b main
  git add -A
  git commit -m "Add textkit playground"
  git push -u origin main
fi
git fetch origin

created=()
for pr in "$demo"/prs/*/; do
  branch="$(tr -d '\r\n' < "$pr/branch.txt")"
  title="$(tr -d '\r\n' < "$pr/title.txt")"
  if [[ -n "$(git ls-remote --heads origin "$branch")" ]]; then
    echo "Branch '$branch' already exists; skipping."
    continue
  fi
  git checkout -B "$branch" origin/main
  cp -R "$pr/changes/." "$work/"
  git add -A
  git commit -m "$title"
  git push -u origin "$branch"
  created+=("$pr")
  echo "Open the PR: https://github.com/$repo/compare/main...$branch?expand=1"
done

if [[ ${#created[@]} -gt 0 ]] && command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  read -r -p "GitHub CLI found. Type yes to open the ${#created[@]} pull request(s) now: " open
  if [[ "$open" == "yes" ]]; then
    for pr in "${created[@]}"; do
      gh pr create --repo "$repo" --base main --head "$(tr -d '\r\n' < "$pr/branch.txt")" \
        --title "$(tr -d '\r\n' < "$pr/title.txt")" --body-file "$pr/body.md"
    done
  fi
fi
echo "Done. The temporary clone is at $work (you may delete it)."
