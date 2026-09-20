#!/usr/bin/env bash
set -euo pipefail

revision=449966c885208d41f995d09c54072e012df9eb1a
source_dir="${RUNNER_TEMP:?RUNNER_TEMP must be set}/inkbox-sdk"
wheel_dir="$RUNNER_TEMP/inkbox-sdk-wheel"
git init "$source_dir"
git -C "$source_dir" fetch --depth=1 https://github.com/inkbox-ai/inkbox "$revision"
git -C "$source_dir" checkout --detach FETCH_HEAD
test "$(git -C "$source_dir" rev-parse HEAD)" = "$revision"
uv build --wheel --out-dir "$wheel_dir" "$source_dir/sdk/python"
wheel="$wheel_dir/inkbox-0.7.3-py3-none-any.whl"
test -f "$wheel"

{
  echo "INKBOX_SDK_WHEEL=$wheel"
  echo "UV_FIND_LINKS=$wheel_dir"
  echo "PIP_FIND_LINKS=$wheel_dir"
} >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
