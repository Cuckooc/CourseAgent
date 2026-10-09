CREATE TABLE IF NOT EXISTS chat_feedback (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL COMMENT '用户ID',
    session_id INT NOT NULL COMMENT '会话ID',
    message_index INT NOT NULL COMMENT '对话轮次（从0开始）',
    rating SMALLINT NOT NULL COMMENT '1=点赞, -1=点踩',
    comment VARCHAR(500) DEFAULT NULL COMMENT '可选文字反馈',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_fb_user_session (user_id, session_id)
) ENGINE=INNODB DEFAULT CHARSET=utf8mb4;
