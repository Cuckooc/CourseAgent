-- 软删除迁移（Phase 4: 数据安全）
-- 执行方式: mysql -u <user> -p <database> < migrations/003_soft_delete.sql
-- 为 4 张业务表添加 is_deleted + deleted_at 列，支持软删除与定时清理。

ALTER TABLE user_information
    ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    ADD COLUMN deleted_at DATETIME NULL COMMENT '软删除时间';

ALTER TABLE history_information
    ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    ADD COLUMN deleted_at DATETIME NULL COMMENT '软删除时间';

ALTER TABLE session_information
    ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    ADD COLUMN deleted_at DATETIME NULL COMMENT '软删除时间';

ALTER TABLE user_profile
    ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    ADD COLUMN deleted_at DATETIME NULL COMMENT '软删除时间';

CREATE TABLE IF NOT EXISTS account_deletion_schedule (
    user_id INT PRIMARY KEY COMMENT '待注销用户ID',
    scheduled_at DATETIME NOT NULL COMMENT '计划硬删除时间（注销后 + 7 天）',
    requested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注销请求时间',
    CONSTRAINT fk_deletion_schedule_user FOREIGN KEY (user_id) REFERENCES user_information(id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='账号注销计划表（7 天冷静期后硬删除）';
