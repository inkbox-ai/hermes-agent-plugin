#!/usr/bin/env bash
# Build the reviewed public SDK candidate; failures never fall back to a registry version.
set -euo pipefail
revision=5bf65d4dfa6d8cfe65ec13a39ffa89a66ecb6b97
root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/inkbox-sdk-${revision}"
if [[ ! -d "$root/source/.git" ]]; then
  mkdir -p "$root"
  git init -q "$root/source" >&2
  git -C "$root/source" remote add origin https://github.com/inkbox-ai/inkbox.git
  git -C "$root/source" fetch --depth 1 origin "$revision" >&2
  git -C "$root/source" checkout --detach FETCH_HEAD >&2
fi
[[ "$(git -C "$root/source" rev-parse HEAD)" == "$revision" ]]
wheel="$root/wheels/inkbox-0.7.21-py3-none-any.whl"
if [[ ! -f "$wheel" ]]; then
  uv build --wheel --out-dir "$root/wheels" "$root/source/sdk/python" >&2
fi
[[ -f "$wheel" ]]
if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf 'UV_FIND_LINKS=%s\n' "$root/wheels" >> "$GITHUB_ENV"
fi
printf '%s\n' "$wheel"
