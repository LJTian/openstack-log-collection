#!/usr/bin/env bash
set -euo pipefail

# 默认参数
DSN_DEFAULT="${DSN:-mysql+asyncmy://root:pass@host.docker.internal:3306/ops?charset=utf8mb4}"
TABLE_DEFAULT="${TABLE:-nova_compute_action_log}"
INCLUDE_DELETE_FLAG="${INCLUDE_DELETE:-1}"
DERIVE_WINDOW_SEC="${DERIVE_WINDOW_SEC:-120}"

# 日志路径（容器外挂载进来）
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

