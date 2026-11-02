#!/usr/bin/env bash
# Run from Git Bash on the computer where GitHub CLI is authenticated.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

pf_repo="FortranFour/power-fingerprint"
pf_tag="v0.7.4"

for pf_command in git gh; do
  if ! command -v "$pf_command" >/dev/null 2>&1; then
    echo "Required command is missing: $pf_command" >&2
    exit 1
  fi
done
if ! gh auth status >/dev/null 2>&1; then
  echo "Run gh auth login, then run this script again." >&2
  exit 1
fi
pf_login="$(gh api user --jq .login)"
if [[ "${pf_login,,}" != "fortranfour" ]]; then
  echo "The active GitHub account is $pf_login. Switch to FortranFour first." >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  git init -b main
fi
if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Publish from the main branch in this extracted source directory." >&2
  exit 1
fi
pf_origin="$(git remote get-url origin 2>/dev/null || true)"
if [[ -n "$pf_origin" ]]; then
  case "$pf_origin" in
    "https://github.com/$pf_repo"|"https://github.com/$pf_repo.git"|"git@github.com:$pf_repo.git") ;;
    *) echo "This directory has a different origin: $pf_origin" >&2; exit 1 ;;
  esac
elif gh repo view "$pf_repo" --json nameWithOwner >/dev/null 2>&1; then
  echo "$pf_repo already exists. This script will not replace an unrelated repository." >&2
  exit 1
fi

if ! git config user.name >/dev/null 2>&1; then
  git config user.name "$pf_login"
fi
if ! git config user.email >/dev/null 2>&1; then
  pf_user_id="$(gh api user --jq .id)"
  git config user.email "$pf_user_id+$pf_login@users.noreply.github.com"
fi

# Publish only the reviewed source tree. Diagnostics and HA configuration are
# outside this allowlist and are not part of the supplied repository bundle.
git add -- custom_components tests docs extras scripts .github \
  .gitignore .gitattributes README.md CHANGELOG.md LICENSE hacs.json publish.sh
if ! git diff --cached --quiet; then
  git commit -m "Prepare Power Fingerprint 0.7.4 for HACS"
fi
gh auth setup-git
if [[ -z "$pf_origin" ]]; then
  gh repo create "$pf_repo" --public --source=. --remote=origin --push \
    --description "Learn power and energy profiles for unmetered devices in Home Assistant"
else
  pf_visibility="$(gh repo view "$pf_repo" --json visibility --jq .visibility)"
  if [[ "$pf_visibility" != "PUBLIC" ]]; then
    echo "HACS requires a public repository. Current visibility is $pf_visibility." >&2
    exit 1
  fi
  git push -u origin main
fi
gh repo edit "$pf_repo" --add-topic home-assistant --add-topic hacs --add-topic energy-monitoring

if gh release view "$pf_tag" --repo "$pf_repo" >/dev/null 2>&1; then
  echo "Release $pf_tag is already published: https://github.com/$pf_repo/releases/tag/$pf_tag"
  exit 0
fi

# Wait for the workflow associated with the exact pushed commit before release.
pf_head="$(git rev-parse HEAD)"
pf_run=""
for ((pf_attempt=0; pf_attempt<15; pf_attempt++)); do
  pf_run="$(gh run list --repo "$pf_repo" --workflow validate.yml --commit "$pf_head" \
    --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true)"
  if [[ -n "$pf_run" ]]; then break; fi
  sleep 2
done
if [[ -z "$pf_run" ]]; then
  echo "The repository was created, but validation has not appeared yet." >&2
  echo "Enable or run the Validate workflow in GitHub Actions, then rerun this script." >&2
  exit 1
fi
gh run watch "$pf_run" --repo "$pf_repo" --exit-status
gh release create "$pf_tag" --repo "$pf_repo" --target "$pf_head" \
  --title "Power Fingerprint 0.7.4" --notes-file docs/RELEASE_0.7.4.md --latest
echo "Published: https://github.com/$pf_repo"
echo "Add that URL to HACS as a custom Integration repository."
