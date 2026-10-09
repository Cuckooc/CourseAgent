CREATE TABLE IF NOT EXISTS chain_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT DEFAULT NULL COMMENT '用户ID',
    session_id INT DEFAULT NULL COMMENT '会话ID',
    task_id VARCHAR(20) DEFAULT NULL COMMENT '请求任务ID',
    log_data LONGTEXT NOT NULL COMMENT 'JSON 序列化的 chain_log',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cl_user_session_time (user_id, session_id, created_at)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4;
