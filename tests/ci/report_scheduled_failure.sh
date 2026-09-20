#!/usr/bin/env bash

set -euo pipefail

mode="${1:-}"
repo_slug="${REPOSITORY//\//-}"
thread_key="ci-${repo_slug}-${RUN_ID}-${RUN_ATTEMPT}"

if [[ "$mode" == "notification" ]]; then
  [[ -n "${NOTIFICATION_URL:-}" ]] || { echo "::error::Missing failure notification URL"; exit 1; }
  payload="$(jq -n --arg repository "${REPOSITORY}" --arg workflow "${WORKFLOW_NAME}" --arg run_url "${RUN_URL}" '{text: ("Scheduled integration checks failed\n\nRepository: " + $repository + "\nWorkflow: " + $workflow + "\nRun: " + $run_url)}')"
  curl --fail-with-body --silent --show-error --retry 3 --retry-connrefused --retry-delay 2 --retry-max-time 90 --connect-timeout 10 --max-time 30 -X POST "${NOTIFICATION_URL}&threadKey=${thread_key}&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" -H 'Content-Type: application/json' --data "${payload}"
elif [[ "$mode" == "event" ]]; then
  [[ -n "${RECEIVER_URL:-}" ]] || { echo "::error::Missing failure receiver URL"; exit 1; }
  [[ -n "${SIGNING_SECRET:-}" ]] || { echo "::error::Missing failure signing secret"; exit 1; }
  [[ -n "${FAILURE_ENVIRONMENT:-}" ]] || { echo "::error::Missing failure environment"; exit 1; }
  echo "::add-mask::${SIGNING_SECRET}"
  payload="$(jq -c -n --arg event_type "scheduled_ci_failure" --arg source "${REPOSITORY}" --arg repository "${REPOSITORY}" --arg workflow "${WORKFLOW_NAME}" --arg source_job "${WORKFLOW_NAME}" --arg environment "${FAILURE_ENVIRONMENT}" --argjson run_id "${RUN_ID}" --argjson run_attempt "${RUN_ATTEMPT}" --arg run_url "${RUN_URL}" --arg head_sha "${HEAD_SHA}" --arg chat_thread_key "${thread_key}" '{$event_type, $source, $repository, $workflow, $source_job, $environment, $run_id, $run_attempt, $run_url, $head_sha, $chat_thread_key}')"
  signature="$(printf '%s' "${payload}" | openssl dgst -sha256 -hmac "${SIGNING_SECRET}" -binary | xxd -p -c 256)"
  request_id="ci:${REPOSITORY}:${RUN_ID}:${RUN_ATTEMPT}:${FAILURE_ENVIRONMENT}"
  curl --fail-with-body --silent --show-error --retry 3 --retry-connrefused --retry-delay 2 --retry-max-time 90 --connect-timeout 10 --max-time 30 -X POST "${RECEIVER_URL}" -H 'Content-Type: application/json' -H 'X-GitHub-Event: workflow_run' -H "X-Hub-Signature-256: sha256=${signature}" -H "X-Inkbox-Request-Id: ${request_id}" --data-binary "${payload}"
else
  echo "usage: $0 {notification|event}" >&2
  exit 2
fi
