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


