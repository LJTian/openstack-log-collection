#!/usr/bin/env bash
set -u -o pipefail

# 说明：
# - 串行执行 vm / glance / neutron / heat 四个模式
# - 任一步骤失败不退出，继续执行后续步骤；最终以最后一步的返回码结束
# - 请按需通过环境变量覆盖以下参数（crontab 中可直接设定）

IMAGE="${IMAGE:-olc-vm-actions:latest}"
DSN="${DSN:-}"  # 必填，例：mysql+asyncmy://user:pass@10.0.0.10:3306/ops?charset=utf8mb4

# 各服务日志目录（宿主机路径）
NOVA_LOG_DIR="${NOVA_LOG_DIR:-/var/log/nova}"
GLANCE_LOG_DIR="${GLANCE_LOG_DIR:-/var/log/glance}"
NEUTRON_LOG_DIR="${NEUTRON_LOG_DIR:-/var/log/neutron}"
HEAT_LOG_DIR="${HEAT_LOG_DIR:-/var/log/heat}"

# Nova 额外参数
INCLUDE_DELETE="${INCLUDE_DELETE:-1}"
DERIVE_WINDOW_SEC="${DERIVE_WINDOW_SEC:-120}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s %s\n' "$(ts)" "$*"; }

if [[ -z "${DSN}" ]]; then
  log "ERROR: DSN 未设置。请在 crontab 中通过 DSN=... 传入数据库连接串。"
  # 不直接退出，继续尝试以便可观察每步报错
fi

ret=0

run_vm() {
  log "[vm] 开始..."
  docker run --rm \
    -e MODE=vm \
    -e DSN="${DSN}" \
    -e TABLE="nova_compute_action_log" \
    -e INCLUDE_DELETE="${INCLUDE_DELETE}" \
    -e DERIVE_WINDOW_SEC="${DERIVE_WINDOW_SEC}" \
    -e API_LOG_PATTERN="/logs/*nova-api*.log" \
    -e COMPUTE_LOG_PATTERN="/logs/*nova-compute*.log" \
    -v "${NOVA_LOG_DIR}":/logs:ro \
    "${IMAGE}"
  rc=$?; log "[vm] 结束，rc=${rc}"; ret=${rc}
}

run_glance() {
  log "[glance] 开始..."
  docker run --rm \
    -e MODE=glance \
    -e DSN="${DSN}" \
    -e TABLE="glance_image_action_log" \
    -e GLANCE_LOG_PATTERN="/logs/*glance-api*.log" \
    -v "${GLANCE_LOG_DIR}":/logs:ro \
    "${IMAGE}"
  rc=$?; log "[glance] 结束，rc=${rc}"; ret=${rc}
}

run_neutron() {
  log "[neutron] 开始..."
  docker run --rm \
    -e MODE=neutron \
    -e DSN="${DSN}" \
    -e TABLE="neutron_action_log" \
    -e NEUTRON_LOG_PATTERN="/logs/*neutron-server*.log" \
    -v "${NEUTRON_LOG_DIR}":/logs:ro \
    "${IMAGE}"
  rc=$?; log "[neutron] 结束，rc=${rc}"; ret=${rc}
}

run_heat() {
  log "[heat] 开始..."
  docker run --rm \
    -e MODE=heat \
    -e DSN="${DSN}" \
    -e TABLE="heat_action_log" \
    -e HEAT_API_PATTERN="/logs/*heat-api*.log" \
    -e HEAT_ENGINE_PATTERN="/logs/*heat-engine*.log" \
    -v "${HEAT_LOG_DIR}":/logs:ro \
    "${IMAGE}"
  rc=$?; log "[heat] 结束，rc=${rc}"; ret=${rc}
}

run_vm || true
run_glance || true
run_neutron || true
run_heat || true

log "全部完成"
exit ${ret}


