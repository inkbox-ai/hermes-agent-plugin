#!/usr/bin/env bash

set -euo pipefail

hermes_commit="bf0203a794a07aac3ff764972ab9087baa4e4f4e" # v0.22.5
installer_url="https://raw.githubusercontent.com/NousResearch/hermes-agent/${hermes_commit}/scripts/install.sh"
installer_path="${RUNNER_TEMP:?RUNNER_TEMP must be set}/hermes-install.sh"
max_attempts="${HERMES_INSTALL_ATTEMPTS:-4}"
attempt_timeout="${HERMES_INSTALL_TIMEOUT:-120}"

for attempt in $(seq 1 "$max_attempts"); do
  if curl --fail --silent --show-error --location \
      --connect-timeout 15 --max-time 60 \
      --retry 3 --retry-all-errors --retry-delay 5 --retry-max-time 90 \
      "$installer_url" --output "$installer_path" \
    && timeout --kill-after=10 "$attempt_timeout" bash "$installer_path" --non-interactive "$@" --commit "$hermes_commit"; then
    exit 0
  fi

  if [[ "$attempt" -lt "$max_attempts" ]]; then
    delay=$((attempt * 15))
    echo "::warning::Hermes install attempt $attempt failed; retrying in ${delay}s"
    sleep "$delay"
  fi
done

echo "::error::Hermes install failed after $max_attempts attempts"
exit 1
