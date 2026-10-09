CREATE TABLE IF NOT EXISTS session_keywords (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    session_id INT NOT NULL,
    keywords TEXT NOT NULL COMMENT '累积关键词（顿号分隔）',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    is_deleted TINYINT(1) NOT NULL DEFAULT 0,
    UNIQUE KEY uk_user_session_kw (user_id, session_id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4;
