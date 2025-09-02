-- OpenStack VM 操作提取结果表（含唯一索引避免重复）

CREATE DATABASE IF NOT EXISTS `ops`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE `ops`;

CREATE TABLE IF NOT EXISTS `nova_compute_action_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts` DATETIME(6) NOT NULL,
  `instance` VARCHAR(128) NOT NULL,
  `user_id` VARCHAR(64) NULL,
  `action` VARCHAR(32) NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ts` (`ts`),
  KEY `idx_instance` (`instance`),
  KEY `idx_user_id` (`user_id`),
  UNIQUE KEY `uq_instance_action_ts` (`instance`, `action`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 若表已存在，仅补充唯一索引（执行一次即可）
ALTER TABLE `nova_compute_action_log`
  ADD UNIQUE KEY `uq_instance_action_ts` (`instance`, `action`, `ts`);

-- Glance 镜像操作日志表

CREATE TABLE IF NOT EXISTS `glance_image_action_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts` DATETIME(6) NOT NULL,
  `image_id` VARCHAR(128) NOT NULL,
  `user_id` VARCHAR(64) NULL,
  `action` VARCHAR(32) NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ts_glance` (`ts`),
  KEY `idx_image_id` (`image_id`),
  KEY `idx_user_id_glance` (`user_id`),
  UNIQUE KEY `uq_image_action_ts` (`image_id`, `action`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 若表已存在，仅补充唯一索引（执行一次即可）
ALTER TABLE `glance_image_action_log`
  ADD UNIQUE KEY `uq_image_action_ts` (`image_id`, `action`, `ts`);

-- Neutron 网络操作日志表

CREATE TABLE IF NOT EXISTS `neutron_action_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts` DATETIME(6) NOT NULL,
  `resource` VARCHAR(64) NOT NULL,
  `resource_id` VARCHAR(128) NOT NULL,
  `user_id` VARCHAR(64) NULL,
  `action` VARCHAR(32) NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ts` (`ts`),
  KEY `idx_resource` (`resource`),
  KEY `idx_resource_id` (`resource_id`),
  KEY `idx_user_id` (`user_id`),
  UNIQUE KEY `uq_neutron_action_ts` (`resource`, `resource_id`, `action`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 若表已存在，仅补充唯一索引（执行一次即可）
ALTER TABLE `neutron_action_log`
  ADD UNIQUE KEY `uq_neutron_action_ts` (`resource`, `resource_id`, `action`, `ts`);

-- Heat 栈操作日志表

CREATE TABLE IF NOT EXISTS `heat_action_log` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts` DATETIME(6) NOT NULL,
  `stack_name` VARCHAR(255) NOT NULL,
  `stack_id` VARCHAR(128) NOT NULL,
  `user_id` VARCHAR(64) NULL,
  `action` VARCHAR(32) NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ts` (`ts`),
  KEY `idx_stack_name` (`stack_name`),
  KEY `idx_stack_id` (`stack_id`),
  KEY `idx_user_id` (`user_id`),
  UNIQUE KEY `uq_heat_action_ts` (`stack_name`, `stack_id`, `action`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 若表已存在，仅补充唯一索引（执行一次即可）
ALTER TABLE `heat_action_log`
  ADD UNIQUE KEY `uq_heat_action_ts` (`stack_name`, `stack_id`, `action`, `ts`);


