-- 007: 用户表增加 token_version 字段，实现单点互踢
-- 登录时 token_version += 1，旧 JWT 的 ver 声明不再匹配 → 立即失效
-- DEFAULT 0 兼容存量用户：旧 token 无 ver 字段时按 0 处理，与库内 0 相等通过
ALTER TABLE user_information
    ADD COLUMN token_version INT NOT NULL DEFAULT 0 COMMENT 'JWT 版本号，登录时+1使旧token失效（单点互踢）';
