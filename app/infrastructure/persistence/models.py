"""
模块名：app.infrastructure.persistence.models

作用：
    ORM 模型定义（SQLAlchemy 2.0 declarative），与 MySQL 8.0 现有表结构
    一一对应，作为表结构的权威定义。当前 DAO 以 text() 原生 SQL 为主，
    模型用于结构约束、自动建表与 Alembic 迁移的 autogenerate 比对。

主要成员（均继承 Base，对应 MySQL 表）：
    UserInformation→user_information、HistoryInformation→history_information、
    SessionInformation→session_information、UserProfile→user_profile、
    DocumentReview→document_review、AccountDeletionSchedule→account_deletion_schedule、
    SessionKeyword→session_keywords、ChatFeedback→chat_feedback、
    ChainLogRecord→chain_log、FileMeta→file_meta。

被谁使用：
    - 由 Base.metadata 统一管理表元数据：migrations/env.py 取
      target_metadata=Base.metadata 供 alembic upgrade head / autogenerate 使用；
      tests/test_db_integrity.py 用 Base 做结构完整性校验；
    - 各 DAO 以表名为目标写 text() 原生 SQL（dao/ 目录），如
      dao/document_review.py 同时 import DocumentReview 做 ORM 读写；
    - 字段上的 comment= 即 MySQL 列注释，索引/唯一键/外键见各 __table_args__。

通用约定：多数表带 is_deleted(0/1) + deleted_at 软删除字段（003 等增量
    SQL 迁移补齐），DAO 查询默认过滤 is_deleted=0。
"""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import declarative_base

# declarative 基类：所有 ORM 类注册到 Base.metadata，供建表与 Alembic 迁移使用
Base = declarative_base()


class UserInformation(Base):
    """用户信息表（账号与鉴权主体），MySQL 表名：user_information。

    由 Base.metadata 建表，被 alembic 迁移（baseline + 0002_user_role 增 role）
    与 dao/user.py、dao/soft_delete.py、dao/profile.py 等使用；JWT 鉴权
    （core/security.py）与 /admin/* 角色校验读取 role。
    索引/约束：user_name、email 各为唯一键；role 默认 user。
    关系去向：id 被 history_information.user_id、session_information.user_id、
    user_profile.user_id（外键）、account_deletion_schedule.user_id（外键）、
    file_meta.user_id 等逻辑引用。
    """
    __tablename__ = "user_information"

    # 主键自增用户 ID；被各业务表以 user_id 引用
    id = Column(Integer, primary_key=True, autoincrement=True, comment="用户ID")
    user_name = Column(String(20), unique=True, comment="用户名")
    user_pwd = Column(String(128), nullable=False, comment="bcrypt密码哈希")
    email = Column(String(255), unique=True, nullable=False, comment="邮箱")
    role = Column(String(16), nullable=False, server_default="user", comment="角色: user/teacher/admin")
    create_time = Column(DateTime, server_default=func.now(), comment="创建/更新时间")
    is_deleted = Column(SmallInteger, nullable=False, server_default="0", comment="软删除标记")
    deleted_at = Column(DateTime, nullable=True, comment="软删除时间")


class HistoryInformation(Base):
    """会话历史（会话清单）表，MySQL 表名：history_information。

    由 Base.metadata 建表，被 alembic baseline 迁移与 dao/history.py、
    dao/session.py、dao/read.py、dao/soft_delete.py 使用；每个用户的每个
    会话一行，侧栏会话列表与标题来自本表，util/title.py 生成的标题写回 title。
    索引/约束：user_id 普通索引；(user_id, session_id) 唯一键 uk_user_session。
    关系去向：user_id 逻辑引用 user_information.id；session_id 与
    session_information.session_id 一对多（本表单条会话头、消息表多条消息），
    无数据库层外键，由 DAO 软保证。
    """
    __tablename__ = "history_information"

    # 自增主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属用户：单列索引，支撑「查某用户全部会话」
    user_id = Column(Integer, nullable=False, index=True, comment="用户ID")
    # 业务会话 ID（与 session_information.session_id 对应）
    session_id = Column(Integer, nullable=False, comment="会话ID")
    # 会话标题：首轮后由 util/title.py 的 LLM 生成并写回
    title = Column(String(100), nullable=False, comment="会话标题")
    # 创建时间：插入时取 MySQL 当前时间
    create_time = Column(DateTime, server_default=func.now())
    # 更新时间：行更新时自动刷新（onupdate）
    update_time = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(SmallInteger, nullable=False, server_default="0", comment="软删除标记")
    deleted_at = Column(DateTime, nullable=True, comment="软删除时间")

    __table_args__ = (
        # 同一用户下会话 ID 唯一，防止会话头重复登记
        UniqueConstraint("user_id", "session_id", name="uk_user_session"),
    )


class SessionInformation(Base):
    """会话消息明细表（一轮一条：user/assistant/system），MySQL 表名：session_information。

    由 Base.metadata 建表，被 alembic baseline 迁移与 dao/information.py、
    dao/session.py、dao/read.py、dao/soft_delete.py 使用；对话历史拼装、
    短期记忆与消息持久化均读写本表。
    索引/约束：idx_user_session_time(user_id, session_id, create_time)
    支撑按会话时间序拉取消息；idx_session_id 支撑仅按会话维度查询。
    关系去向：user_id 逻辑引用 user_information.id；session_id 逻辑对应
    history_information.session_id（无库层外键，DAO 软保证）。
    """
    __tablename__ = "session_information"

    # 自增主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, nullable=False, comment="会话ID")
    user_id = Column(Integer, nullable=False, comment="用户ID")
    # 消息角色：用户提问 user / AI 回答 assistant / 系统注入 system
    role = Column(String(20), nullable=False, comment="角色: user/assistant/system")
    # 消息正文：LONGTEXT 容纳长上下文与长回答
    content = Column(LONGTEXT, nullable=False, comment="会话内容")
    # 创建时间：兼作消息排序游标（索引 idx_user_session_time 含本列）
    create_time = Column(DateTime, server_default=func.now())
    is_deleted = Column(SmallInteger, nullable=False, server_default="0", comment="软删除标记")
    deleted_at = Column(DateTime, nullable=True, comment="软删除时间")

    __table_args__ = (
        # 会话消息主查询路径：用户 + 会话 + 时间范围/排序
        Index("idx_user_session_time", "user_id", "session_id", "create_time"),
        # 仅按会话维度查询/删除时的辅助索引
        Index("idx_session_id", "session_id"),
    )


class UserProfile(Base):
    """用户画像表（长期记忆）：一个用户一条记录，MySQL 表名：user_profile。

    由 Base.metadata 建表，被 alembic 迁移 0003_user_profile（create_table）
    与 dao/profile.py、memory/profile_service.py 使用；画像先在 Redis 暂存，
    间隔/到期后 flush 到本表，个人信息页展示与编辑读写 interests/topics。
    索引/外键：user_id 为主键且为外键 fk_profile_user → user_information.id
    （一对一，随用户删除而清理）。
    """
    __tablename__ = "user_profile"

    user_id = Column(Integer, ForeignKey("user_information.id"), primary_key=True, comment="用户ID")
    # MySQL TEXT 不允许默认值，由 DAO 写入时保证非空
    profile_text = Column(Text, nullable=True, comment="用户画像要点（完整文本）")
    interests = Column(String(500), nullable=False, server_default="", comment="兴趣爱好（顿号分隔）")
    topics = Column(String(500), nullable=False, server_default="", comment="常问主题/关注方向（顿号分隔）")
    create_time = Column(DateTime, server_default=func.now())
    update_time = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(SmallInteger, nullable=False, server_default="0", comment="软删除标记")
    deleted_at = Column(DateTime, nullable=True, comment="软删除时间")


class DocumentReview(Base):
    """文档审核队列表：OCR/多模态提取结果的人工审核队列，MySQL 表名：document_review。

    由 Base.metadata 建表，结构对应增量脚本 db/migrations/002_document_review.sql，
    被 dao/document_review.py（本仓唯一直接 import ORM 类做读写的 DAO）与
    审核相关 service/路由使用；扫描件/双栏/图文密集文档先入队，人工
    approved 后清洗文本才进入向量化入库链路。
    索引：idx_user_status(user_id, status) 支撑「按用户 + 审核状态」筛队列。
    关系去向：user_id 逻辑引用 user_information.id（无库层外键）；
    file_path 对应上传存储目录中的实体文件（与 file_meta 各管一段流程）。
    """
    __tablename__ = "document_review"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True, comment="上传用户ID")
    file_name = Column(String(255), nullable=False, comment="原始文件名")
    file_path = Column(String(500), nullable=False, comment="服务器存储路径")
    doc_type = Column(String(20), nullable=False, comment="文档类型: scanned/image_rich/two_column")
    raw_text = Column(LONGTEXT, nullable=True, comment="OCR/提取原始文本")
    cleaned_text = Column(LONGTEXT, nullable=True, comment="清洗后文本（待审核）")
    status = Column(String(20), nullable=False, server_default="pending",
                    comment="审核状态: pending/approved/rejected")
    reviewer_notes = Column(Text, nullable=True, comment="审核备注/驳回原因")
    created_at = Column(DateTime, server_default=func.now())
    reviewed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_user_status", "user_id", "status"),
    )


class AccountDeletionSchedule(Base):
    """账号注销计划表：用户申请注销后 7 天冷静期，到期由定时任务硬删除，
    MySQL 表名：account_deletion_schedule。

    由 Base.metadata 建表（结构对应增量 SQL 迁移），被 dao/soft_delete.py
    的注销登记/撤销逻辑与 core/purge_scheduler.py 定时清理任务使用：冷静期内
    账号为软删除可恢复，到 scheduled_at 后定时任务硬删用户及其关联数据。
    索引/外键：user_id 为主键且为外键 → user_information.id（一对一）。
    """
    __tablename__ = "account_deletion_schedule"

    user_id = Column(Integer, ForeignKey("user_information.id"), primary_key=True, comment="待注销用户ID")
    scheduled_at = Column(DateTime, nullable=False, comment="计划硬删除时间")
    requested_at = Column(DateTime, nullable=False, server_default=func.now(), comment="注销请求时间")


class SessionKeyword(Base):
    """会话关键词累积表：每 (user_id, session_id) 一行，关键词顿号分隔，
    MySQL 表名：session_keywords。

    由 Base.metadata 建表（对应 db/migrations/004_session_keywords.sql），
    被 dao/session_keyword.py 与 multi_agent/summary_agent.py 的关键词产出
    链路使用；累积关键词注入 ChatLLM 提示词的 {session_keywords}，帮助
    模型锁定会话主题。
    索引/约束：(user_id, session_id) 唯一键 uk_user_session_kw。
    关系去向：user_id 逻辑引用 user_information.id；session_id 逻辑对应
    history_information.session_id（无库层外键）。
    """
    __tablename__ = "session_keywords"

    # 自增主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属用户
    user_id = Column(Integer, nullable=False)
    # 归属会话
    session_id = Column(Integer, nullable=False)
    # 累积关键词文本：顿号分隔（来源 InformationLLM 提取 + DAO 合并去重）
    keywords = Column(Text, nullable=False)
    # 首次写入时间
    create_time = Column(DateTime, server_default=func.now())
    # 最近累积时间：每次合并关键词自动刷新
    update_time = Column(DateTime, server_default=func.now(), onupdate=func.now())
    # 软删除标记（本表无 deleted_at，随会话删除置 1）
    is_deleted = Column(SmallInteger, nullable=False, server_default="0")

    __table_args__ = (
        # 同一会话只保留一条关键词累积记录
        UniqueConstraint("user_id", "session_id", name="uk_user_session_kw"),
    )


class ChatFeedback(Base):
    """用户对 AI 回答的反馈表（点赞/点踩 + 可选文字），MySQL 表名：chat_feedback。

    由 Base.metadata 建表（对应 db/migrations/005_chat_feedback.sql），
    被 dao/feedback.py 与反馈接口使用，供后续回答质量离线分析。
    索引：idx_fb_user_session(user_id, session_id) 支撑按会话查反馈。
    关系去向：user_id 逻辑引用 user_information.id；session_id 逻辑对应
    history_information.session_id；message_index 定位会话内具体轮次
    （与 session_information 消息顺序对应，均无库层外键）。
    """
    __tablename__ = "chat_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, comment="用户ID")
    session_id = Column(Integer, nullable=False, comment="会话ID")
    message_index = Column(Integer, nullable=False, comment="对话轮次（从0开始）")
    rating = Column(SmallInteger, nullable=False, comment="1=点赞, -1=点踩")
    comment = Column(String(500), nullable=True, comment="可选文字反馈")
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_fb_user_session", "user_id", "session_id"),
    )


class ChainLogRecord(Base):
    """Agent 链路日志持久化表：供离线分析与调试，MySQL 表名：chain_log。

    由 Base.metadata 建表（对应 db/migrations/006_chain_log.sql），
    被 dao/chain_log.py 写入；multi_agent 状态机/各 Agent 的运行轨迹
    （路由、检索、重试、降级等）序列化为 JSON 存 log_data。
    索引：idx_cl_user_session_time(user_id, session_id, created_at)
    支撑按用户/会话/时间范围回溯链路。
    关系去向：user_id/session_id 逻辑引用用户与会话（可空，未登录场景也记录；
    无库层外键）；task_id 对应一次请求任务标识。
    """
    __tablename__ = "chain_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, comment="用户ID")
    session_id = Column(Integer, nullable=True, comment="会话ID")
    task_id = Column(String(20), nullable=True, comment="请求任务ID")
    log_data = Column(LONGTEXT, nullable=False, comment="JSON 序列化的 chain_log")
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_cl_user_session_time", "user_id", "session_id", "created_at"),
    )


class FileMeta(Base):
    """文件元数据表：上传文件的存储位置、内容哈希、向量化进度等，
    MySQL 表名：file_meta。

    由 Base.metadata 建表（对应 db/migrations/007_token_version.sql 等
    增量脚本的版本演进而定），被 dao/knowledge.py 与 service/file_service.py、
    service/vector_store.py 的入库/去重/版本管理链路使用：content_hash
    判重、status 驱动向量化流程、vector_count 记录入库向量数。
    索引：idx_fm_user(user_id)、idx_fm_status(status)、
    idx_fm_content_hash(content_hash)。
    关系去向：user_id 逻辑引用 user_information.id；session_id 对私有文件
    标记归属会话（公共文件 bucket 另标识）；object_key 对应存储实体文件。

    注：当前表无外键约束（user_id 不强制引用 user_information），
    业务层通过 dao 软保证引用完整性；如后续需要可补 FK。
    """
    __tablename__ = "file_meta"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, comment="上传用户ID")
    session_id = Column(Integer, nullable=True, comment="所属会话ID（私有文件归属）")
    bucket = Column(String(64), nullable=True, comment="存储桶（私有/公共）")
    object_key = Column(String(500), nullable=True, comment="对象存储 key/uuid 文件名")
    filename = Column(String(255), nullable=False, comment="原始文件名")
    size = Column(Integer, nullable=False, server_default="0", comment="文件字节数")
    content_hash = Column(String(64), nullable=False, comment="内容 SHA-256 哈希（去重依据）")
    status = Column(String(20), nullable=False, server_default="pending",
                    comment="处理状态: pending/processing/done/failed")
    error = Column(Text, nullable=True, comment="处理失败原因")
    vector_count = Column(Integer, nullable=False, server_default="0", comment="已入库向量数")
    create_time = Column(DateTime, server_default=func.now(), comment="上传时间")
    update_time = Column(DateTime, server_default=func.now(), onupdate=func.now(), comment="状态更新时间")

    __table_args__ = (
        Index("idx_fm_user", "user_id"),
        Index("idx_fm_status", "status"),
        Index("idx_fm_content_hash", "content_hash"),
    )
