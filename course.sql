-- ============================================================
-- 企业级加固版表结构（MySQL 8.0）
-- 变更点：
--   1. user_pwd 扩容至 VARCHAR(72) 以存储 bcrypt 哈希（旧为 VARCHAR(32)）
--   2. history_information 增加 (user_id, session_id) 唯一键，会话标题幂等 upsert
--   3. session_information 增加 (user_id, session_id, create_time) 索引
--   4. 补充外键约束（InnoDB）
-- 全新部署直接执行本脚本；已有数据库请执行文件末尾的【升级脚本】
-- ============================================================

CREATE DATABASE IF NOT EXISTS db_course DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE db_course;

CREATE TABLE user_information(
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '用户ID，主键自增',
    user_name VARCHAR(20) UNIQUE COMMENT '用户名，唯一约束',
    user_pwd VARCHAR(72) NOT NULL COMMENT '用户密码（bcrypt 哈希，60 字符）',
    email VARCHAR(255) UNIQUE NOT NULL COMMENT '邮箱，唯一且非空',
    role VARCHAR(16) NOT NULL DEFAULT 'user' COMMENT '角色: user/admin（admin 可访问 /admin/* 管理端点）',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '创建/更新时间，自动赋值',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    deleted_at DATETIME NULL COMMENT '软删除时间'
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='用户个人信息表';

CREATE TABLE history_information(
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '历史会话ID，自增主键',
    user_id INT NOT NULL COMMENT '用户ID',
    session_id INT NOT NULL COMMENT '会话id',
    title VARCHAR(100) NOT NULL COMMENT '历史记录的标题',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间，自动赋值',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间，自动赋值',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    deleted_at DATETIME NULL COMMENT '软删除时间',
    UNIQUE KEY uk_user_session (user_id, session_id),
    KEY idx_user_id (user_id),
    CONSTRAINT fk_history_user FOREIGN KEY (user_id) REFERENCES user_information(id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='历史记录表';

CREATE TABLE session_information(
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT 'id',
    session_id INT NOT NULL COMMENT '会话id',
    user_id INT NOT NULL COMMENT '用户id',
    role VARCHAR(20) CHECK(role IN ('user','assistant','system')) NOT NULL COMMENT '角色身份',
    content LONGTEXT NOT NULL COMMENT '会话内容',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间，自动赋值',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    deleted_at DATETIME NULL COMMENT '软删除时间',
    KEY idx_user_session_time (user_id, session_id, create_time),
    KEY idx_session_id (session_id),
    CONSTRAINT fk_session_user FOREIGN KEY (user_id) REFERENCES user_information(id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='会话记录表';

-- 用户画像表（记忆模块：长期保存的用户习惯/画像，一个用户一条记录）
CREATE TABLE user_profile(
    user_id INT PRIMARY KEY COMMENT '用户ID',
    profile_text TEXT COMMENT '用户画像要点（完整文本，LLM提取+人工编辑合并）',
    interests VARCHAR(500) NOT NULL DEFAULT '' COMMENT '兴趣爱好（顿号分隔）',
    topics VARCHAR(500) NOT NULL DEFAULT '' COMMENT '常问主题/关注方向（顿号分隔）',
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    deleted_at DATETIME NULL COMMENT '软删除时间',
    CONSTRAINT fk_profile_user FOREIGN KEY (user_id) REFERENCES user_information(id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='用户画像表（长期记忆）';

-- 账号注销计划表（用户申请注销后 7 天冷静期，到期后由定时任务硬删除）
CREATE TABLE account_deletion_schedule(
    user_id INT PRIMARY KEY COMMENT '待注销用户ID',
    scheduled_at DATETIME NOT NULL COMMENT '计划硬删除时间（注销后 + 7 天）',
    requested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注销请求时间',
    CONSTRAINT fk_deletion_schedule_user FOREIGN KEY (user_id) REFERENCES user_information(id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='账号注销计划表（7 天冷静期后硬删除）';

-- 会话关键词累积表（每会话独立，用于上下文压缩后保留主题锚点，防信息丢失）
CREATE TABLE IF NOT EXISTS session_keywords (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL COMMENT '用户ID',
    session_id INT NOT NULL COMMENT '会话ID',
    keywords TEXT NOT NULL COMMENT '累积关键词（顿号分隔）',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记',
    UNIQUE KEY uk_user_session_kw (user_id, session_id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4 COMMENT='会话关键词累积表（按会话隔离，注入模型输入防压缩丢失）';

-- ============================================================
-- 【升级脚本】已有数据库执行（幂等，可重复执行）
-- ============================================================
-- 1. 密码列扩容（bcrypt 哈希 60 字符）
-- ALTER TABLE user_information MODIFY COLUMN user_pwd VARCHAR(72) NOT NULL COMMENT '用户密码（bcrypt 哈希）';
--
-- 2. 会话标题列扩容
-- ALTER TABLE history_information MODIFY COLUMN title VARCHAR(100) NOT NULL COMMENT '历史记录的标题';
--
-- 3. 历史表唯一键（若已存在重复 (user_id,session_id) 需先清理重复数据）
-- ALTER TABLE history_information ADD UNIQUE KEY uk_user_session (user_id, session_id);
-- ALTER TABLE history_information ADD KEY idx_user_id (user_id);
--
-- 4. 会话消息表索引
-- ALTER TABLE session_information ADD KEY idx_user_session_time (user_id, session_id, create_time);
-- ALTER TABLE session_information ADD KEY idx_session_id (session_id);
--
-- 5. 历史明文密码：用户下次使用旧密码登录成功后会自动升级为 bcrypt 哈希；
--    也可由管理员重置密码。
--
-- 6. 用户角色列（2026-09-12：RBAC 前置，/admin/* 端点要求 admin 角色）
-- ALTER TABLE user_information ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'user' COMMENT '角色: user/admin';
-- 引导首个管理员：UPDATE user_information SET role = 'admin' WHERE user_name = '<管理员用户名>';
