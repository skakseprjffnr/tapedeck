#!/usr/bin/env bash
# Attaches files to this tag's release. The Windows and Android builds run at the same time,
# so whichever finishes first creates the release, and the other one adds to it.
set -euo pipefail
tag="${GITHUB_REF_NAME:?}"
for attempt in 1 2 3 4 5; do
  gh release view "$tag" > /dev/null 2>&1 ||
    gh release create "$tag" --title "$tag" --notes "" --verify-tag > /dev/null 2>&1 || true
  if gh release upload "$tag" "$@" --clobber; then
    exit 0
  fi
  sleep $((attempt * 10))
done
echo "Couldn't attach the files to release $tag" >&2
exit 1
