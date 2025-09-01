SHELL := /bin/bash

# ---------- 可配置变量 ----------
PY := python3
PIP := pip
VENV := .venv
ACTIVATE := source $(VENV)/bin/activate

IMAGE ?= olc-vm-actions:latest

DSN ?= mysql+asyncmy://root:pass@127.0.0.1:3306/ops?charset=utf8mb4
TABLE ?= nova_compute_action_log
INCLUDE_DELETE ?= 1
DERIVE_WINDOW_SEC ?= 120
API_LOG_PATTERN ?= /var/log/nova/*nova-api*.log
COMPUTE_LOG_PATTERN ?= /var/log/nova/*nova-compute*.log
LOGS_DIR ?= /var/log/nova

MYSQL_HOST ?= 127.0.0.1
MYSQL_PORT ?= 3306
MYSQL_USER ?= root
MYSQL_PASSWORD ?= pass

ifeq ($(INCLUDE_DELETE),1)
INCLUDE_FLAG := --include-delete
else
INCLUDE_FLAG :=
endif

.PHONY: help venv dry-run run db-init docker-build docker-run clean

help:
	@echo "可用目标："
	@echo "  venv         - 创建并安装依赖到本地虚拟环境"
	@echo "  dry-run      - 本地 dry-run 解析并打印所有记录"
	@echo "  run          - 本地解析并写入数据库（唯一索引去重）"
	@echo "  db-init      - 用 mysql 客户端执行 db/schema.sql 初始化库表"
	@echo "  docker-build - 构建镜像 $(IMAGE)"
	@echo "  docker-run   - 以容器方式运行（挂载日志目录 $(LOGS_DIR) 到 /logs）"
	@echo "  clean        - 清理虚拟环境与缓存文件"

venv:
	$(PY) -m venv $(VENV)
	$(ACTIVATE) && $(PIP) install -r requirements.txt

dry-run: venv
	$(ACTIVATE) && \
	python -m scripts.extract_vm_actions \
	  --dry-run \
	  $(INCLUDE_FLAG) \
	  --derive-window-sec $(DERIVE_WINDOW_SEC) \
	  --pattern "$(API_LOG_PATTERN)" \
	  --pattern "$(COMPUTE_LOG_PATTERN)"

run: venv
	$(ACTIVATE) && \
	python -m scripts.extract_vm_actions \
	  $(INCLUDE_FLAG) \
	  --derive-window-sec $(DERIVE_WINDOW_SEC) \
	  --dsn "$(DSN)" \
	  --table "$(TABLE)" \
	  --pattern "$(API_LOG_PATTERN)" \
	  --pattern "$(COMPUTE_LOG_PATTERN)"

db-init:
	@echo "初始化数据库 schema 到 $(MYSQL_HOST):$(MYSQL_PORT) ..."
	@mysql -h $(MYSQL_HOST) -P $(MYSQL_PORT) -u$(MYSQL_USER) -p$(MYSQL_PASSWORD) < db/schema.sql

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm \
	  -e DSN="$(DSN)" \
	  -e TABLE="$(TABLE)" \
	  -e INCLUDE_DELETE=$(INCLUDE_DELETE) \
	  -e DERIVE_WINDOW_SEC=$(DERIVE_WINDOW_SEC) \
	  -e API_LOG_PATTERN="$(API_LOG_PATTERN)" \
	  -e COMPUTE_LOG_PATTERN="$(COMPUTE_LOG_PATTERN)" \
	  -v $(LOGS_DIR):/logs:ro \
	  $(IMAGE)

clean:
	rm -rf $(VENV) __pycache__ .pytest_cache
	find . -name "*.pyc" -delete -o -name "*.pyo" -delete -o -name "*.DS_Store" -delete


