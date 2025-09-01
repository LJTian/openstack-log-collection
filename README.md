## OpenStack VM 操作提取脚本

本仓库已精简为单脚本：从 `nova-api.log` 与 `nova-compute.log` 中提取 VM 操作（创建/删除/暂停/恢复/打开控制台），写入数据库或以 dry-run 方式打印。

### 安装依赖
```bash
pip install -r requirements.txt
```

### 初始化数据库（一次性）
- 使用本机 mysql 客户端：
```bash
mysql -h 127.0.0.1 -uroot -ppass < db/schema.sql
```

- 若使用 docker 启动的 MariaDB/MySQL（容器名假设为 mariadb）：
```bash
docker exec -i mariadb mysql -uroot -ppass < db/schema.sql
```

说明：脚本采用唯一索引 `uq_instance_action_ts(instance,action,ts)` + INSERT IGNORE，支持多次执行且不写入重复记录。

### 使用
- Dry-run 打印（从 config.yaml 读取日志路径与 DSN；或用 --pattern 指定）：
```bash
python -m scripts.extract_vm_actions --dry-run --include-delete --derive-window-sec 120 \
  --pattern "/var/log/nova/*nova-api*.log" \
  --pattern "/var/log/nova/*nova-compute*.log"
```

- 容器运行
```bash
# 1) 构建镜像
docker build -t olc-vm-actions:latest .

# 2) 运行（挂载日志目录 /logs；按需修改 DSN/TABLE 等环境变量）
docker run --rm \
  -e DSN="mysql+asyncmy://root:pass@host.docker.internal:3306/ops?charset=utf8mb4" \
  -e TABLE="nova_compute_action_log" \
  -e INCLUDE_DELETE=1 \
  -e DERIVE_WINDOW_SEC=120 \
  -v /var/log/nova:/logs:ro \
  olc-vm-actions:latest
```

### 容器多模式（VM/Glance/Neutron）
- VM（Nova）模式：
```bash
docker run --rm \
  -e MODE=vm \
  -e DSN="mysql+asyncmy://root:pass@host.docker.internal:3306/ops?charset=utf8mb4" \
  -e TABLE="nova_compute_action_log" \
  -e INCLUDE_DELETE=1 \
  -e DERIVE_WINDOW_SEC=120 \
  -e API_LOG_PATTERN="/logs/*nova-api*.log" \
  -e COMPUTE_LOG_PATTERN="/logs/*nova-compute*.log" \
  -v /var/log/nova:/logs:ro \
  olc-vm-actions:latest
```

- Glance 模式：
```bash
docker run --rm \
  -e MODE=glance \
  -e DSN="mysql+asyncmy://root:pass@host.docker.internal:3306/ops?charset=utf8mb4" \
  -e TABLE="glance_image_action_log" \
  -e GLANCE_LOG_PATTERN="/logs/*glance-api*.log" \
  -v /var/log/glance:/logs:ro \
  olc-vm-actions:latest
```

- Neutron 模式：
```bash
docker run --rm \
  -e MODE=neutron \
  -e DSN="mysql+asyncmy://root:pass@host.docker.internal:3306/ops?charset=utf8mb4" \
  -e TABLE="neutron_action_log" \
  -e NEUTRON_LOG_PATTERN="/logs/*neutron-server*.log" \
  -v /var/log/neutron:/logs:ro \
  olc-vm-actions:latest
```

- 写入数据库（会先清空目标表）：
```bash
python -m scripts.extract_vm_actions --include-delete --derive-window-sec 120 \
  --dsn "mysql+asyncmy://user:pass@host:3306/ops?charset=utf8mb4" \
  --table nova_compute_action_log \
  --pattern "/var/log/nova/*nova-api*.log" \
  --pattern "/var/log/nova/*nova-compute*.log"
```

### 动作识别规则（简要）
- 创建：`POST /servers`（无实例 ID 路径段）
- 删除：`DELETE /servers/<uuid>`（需 `--include-delete`）
- 打开控制台：`POST /servers/<uuid>/remote-consoles`
- 暂停/恢复：当 API 仅出现 `POST /servers/<uuid>/action` 时，通过以下方式判定：
  - 优先用相同 req-id 对齐 compute 的 `VM Paused/Resumed (Lifecycle Event)`
  - 其次用同实例的时间窗口（`--derive-window-sec` 可调，默认 60s）对齐最近的 `Paused/Resumed`

### 注意
- 样例日志中“VM Paused”多为创建/迁移的自动事件，通常不计入“用户暂停”。
