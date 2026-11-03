#!/usr/bin/env bash
# Fails unless the tag (e.g. v1.0.0) equals "v" + the version in pyproject.toml.
set -euo pipefail

tag="${1:?usage: check-tag-version.sh TAG [PYPROJECT]}"
pyproject="${2:-pyproject.toml}"
version="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$pyproject")"

if [ "$tag" != "v$version" ]; then
  echo "::error::Tag $tag does not match pyproject.toml version $version (expected v$version)" >&2
  exit 1
fi
echo "Tag $tag matches pyproject.toml version $version"
