#!/bin/bash
set -euo pipefail
base=$1
root=$(pwd -P)
git reset --hard "$base"
git clean -fdq
git checkout --detach "$base"
# Materialize and sanitize each initialized submodule first, including nested ones.
# Its desired commit comes from the parent's BASE tree, never the image's future HEAD.
git submodule foreach 'bash /swe/sanitize-git.sh "$sha1"'
git branch --force pro-visible-base "$base"
tmp=$(mktemp -d /tmp/pro-visible.XXXXXX)
git -c protocol.file.allow=always clone --bare --single-branch --branch pro-visible-base --no-local "$root" "$tmp/repo.git"
rm -rf -- "$root/.git"
mv "$tmp/repo.git" "$root/.git"
rmdir "$tmp"
git config core.bare false
git reset --mixed "$base"
git remote remove origin
git submodule init
test "$(git rev-parse HEAD)" = "$base"
test -z "$(git remote)"
git fsck --full --no-reflogs --unreachable
# No original parent .git/modules or remote/future objects are retained.
git status --porcelain
