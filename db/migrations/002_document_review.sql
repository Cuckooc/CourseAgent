-- 文档审核表迁移（Phase 2: 人工审核系统）
-- 执行方式: mysql -u <user> -p <database> < migrations/002_document_review.sql

CREATE TABLE IF NOT EXISTS document_review (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL COMMENT '上传用户ID',
    file_name VARCHAR(255) NOT NULL COMMENT '原始文件名',
    file_path VARCHAR(500) NOT NULL COMMENT '服务器存储路径',
    doc_type VARCHAR(20) NOT NULL COMMENT '文档类型: scanned/image_rich/two_column',
    raw_text LONGTEXT COMMENT 'OCR/提取原始文本',
    cleaned_text LONGTEXT COMMENT '清洗后文本（待审核）',
    status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '审核状态: pending/approved/rejected',
    reviewer_notes TEXT COMMENT '审核备注/驳回原因',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    reviewed_at DATETIME NULL,
    INDEX idx_user_status (user_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
