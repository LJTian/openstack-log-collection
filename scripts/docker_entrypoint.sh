#!/usr/bin/env bash
set -euo pipefail

# 运行模式：vm | glance | neutron
MODE="${MODE:-vm}"

# 通用参数
DSN_DEFAULT="${DSN:-mysql+asyncmy://root:pass@host.docker.internal:3306/ops?charset=utf8mb4}"

if [[ "$MODE" == "vm" ]]; then
  TABLE_DEFAULT="${TABLE:-nova_compute_action_log}"
  INCLUDE_DELETE_FLAG="${INCLUDE_DELETE:-1}"
  DERIVE_WINDOW_SEC="${DERIVE_WINDOW_SEC:-120}"
  API_LOG_PATTERN=${API_LOG_PATTERN:-/logs/*nova-api*.log}
  COMPUTE_LOG_PATTERN=${COMPUTE_LOG_PATTERN:-/logs/*nova-compute*.log}

  ARGS=(
    --derive-window-sec "${DERIVE_WINDOW_SEC}"
    --dsn "${DSN_DEFAULT}"
    --table "${TABLE_DEFAULT}"
    --pattern "${API_LOG_PATTERN}"
    --pattern "${COMPUTE_LOG_PATTERN}"
  )
  if [[ "${INCLUDE_DELETE_FLAG}" == "1" ]]; then
    ARGS+=(--include-delete)
  fi
  exec python -m scripts.extract_vm_actions "${ARGS[@]}"
else
  if [[ "$MODE" == "glance" ]]; then
    TABLE_DEFAULT="${TABLE:-glance_image_action_log}"
    GLANCE_LOG_PATTERN=${GLANCE_LOG_PATTERN:-/logs/*glance-api*.log}

    ARGS=(
      --dsn "${DSN_DEFAULT}"
      --table "${TABLE_DEFAULT}"
      --pattern "${GLANCE_LOG_PATTERN}"
    )
    exec python -m scripts.extract_glance_actions "${ARGS[@]}"
  else
    # neutron 模式
    TABLE_DEFAULT="${TABLE:-neutron_action_log}"
    NEUTRON_LOG_PATTERN=${NEUTRON_LOG_PATTERN:-/logs/*neutron-server*.log}

    ARGS=(
      --dsn "${DSN_DEFAULT}"
      --table "${TABLE_DEFAULT}"
      --pattern "${NEUTRON_LOG_PATTERN}"
    )
    exec python -m scripts.extract_neutron_actions "${ARGS[@]}"
  fi
fi

