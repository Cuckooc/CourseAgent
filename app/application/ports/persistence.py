"""
模块名：app.application.ports.persistence

作用：
    MySQL 持久化能力（DAO）的运行时装配点（服务定位器）。api/application/
    domain/auth 层的会话、消息、用户、反馈、知识库、审核等数据读写统一经
    本模块获取 DAO 实例或事务作用域，不直接 import
    app.infrastructure.persistence.*（分层守卫 RULES 禁止业务层依赖基础
    设施）；具体实现由组合根 app/api/deps.py 在应用启动时注册
    （Port↔Adapter 装配）。

主要成员（register_xxx_dao 由组合根启动时调用一次；get_xxx_dao() 返回
    新建的 DAO 实例，语义与调用方原 ``XxxDAO()`` 完全一致——既有 DAO 均
    为无状态，按需实例化）：
    - get_session_dao()：会话管理 DAO（history_information /
      session_information，对应 infrastructure SessionDAO）；
    - get_read_dao()：用户/会话只读 DAO（Information_Read）；
    - get_user_dao()：用户账号写入 DAO（user.Information，与
      information.Information 同名不同类，Port 层以 user_dao 区分）；
    - get_information_dao()：会话消息写入 DAO（information.Information）；
    - get_history_dao()：会话历史/标题写入 DAO（Information_history）；
    - get_feedback_dao()：用户反馈 DAO（FeedbackDAO）；
    - get_knowledge_dao()：知识库文档 DAO（KnowledgeDAO；实现构造需
      upload_dir，组合根注册零参工厂内部闭包 settings.UPLOAD_DIR）；
    - get_chain_log_dao()：Agent 链路日志 DAO（ChainLogDAO）；
    - get_profile_dao()：用户画像 DAO（ProfileDAO）；
    - get_session_keyword_dao()：会话关键词 DAO（SessionKeywordDAO）；
    - get_document_review_dao()：文档审核 DAO（DocumentReviewDAO）；
    - build_stored_filename(...)：上传文件落盘命名（模块函数门面）；
    - recover_last_deleted(user_id)：撤销最近一次软删除（模块函数门面）；
    - session_scope()：ORM 事务作用域上下文管理器（门面）。

被谁使用：
    - 调用方：app/api/v1/{admin,auth,chat,files,history}.py、
      app/application/{admin/admin_user_service,auth/user,chat/agent_service,
      chat/chat_service,knowledge/knowledge_service,review/review_service}.py、
      app/auth/guards.py、app/domain/memory/{context_memory,long_term,
      profile_service,session_keyword_service,session_rollover}.py；
    - 装配方：app/api/deps.py（import 时执行各 register_*）。
"""
from typing import Any, Callable, Optional

__all__ = [
    "get_session_dao",
    "register_session_dao",
    "get_read_dao",
    "register_read_dao",
    "get_user_dao",
    "register_user_dao",
    "get_information_dao",
    "register_information_dao",
    "get_history_dao",
    "register_history_dao",
    "get_feedback_dao",
    "register_feedback_dao",
    "get_knowledge_dao",
    "register_knowledge_dao",
    "get_chain_log_dao",
    "register_chain_log_dao",
    "get_profile_dao",
    "register_profile_dao",
    "get_session_keyword_dao",
    "register_session_keyword_dao",
    "get_document_review_dao",
    "register_document_review_dao",
    "build_stored_filename",
    "register_build_stored_filename",
    "recover_last_deleted",
    "register_recover_last_deleted",
    "session_scope",
    "register_session_scope",
]


# 已注册的 DAO 工厂/函数实现（组合根装配前为 None）
_session_dao: Optional[Callable[[], Any]] = None
_read_dao: Optional[Callable[[], Any]] = None
_user_dao: Optional[Callable[[], Any]] = None
_information_dao: Optional[Callable[[], Any]] = None
_history_dao: Optional[Callable[[], Any]] = None
_feedback_dao: Optional[Callable[[], Any]] = None
_knowledge_dao: Optional[Callable[[], Any]] = None
_chain_log_dao: Optional[Callable[[], Any]] = None
_profile_dao: Optional[Callable[[], Any]] = None
_session_keyword_dao: Optional[Callable[[], Any]] = None
_document_review_dao: Optional[Callable[[], Any]] = None
_build_stored_filename: Optional[Callable[..., str]] = None
_recover_last_deleted: Optional[Callable[[int], Optional[str]]] = None
_session_scope: Optional[Callable[[], Any]] = None


def _unloaded(what: str, registrar: str) -> RuntimeError:
    """组装「未装配」错误：组合根未注册实现属启动期配置错误。"""
    return RuntimeError(
        "持久化能力未装配：组合根 app/api/deps.py 未注册{}（{}）".format(what, registrar)
    )


# ---------------------------------------------------------------------------
# 会话管理 DAO（SessionDAO）
# ---------------------------------------------------------------------------

def register_session_dao(factory: Callable[[], Any]) -> None:
    """注册会话管理 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _session_dao
    _session_dao = factory


def get_session_dao() -> Any:
    """获取会话管理 DAO 实例（语义同 infrastructure.repositories.session.SessionDAO()）。"""
    if _session_dao is None:
        raise _unloaded("会话 DAO", "register_session_dao")
    return _session_dao()


# ---------------------------------------------------------------------------
# 用户/会话只读 DAO（Information_Read）
# ---------------------------------------------------------------------------

def register_read_dao(factory: Callable[[], Any]) -> None:
    """注册只读查询 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _read_dao
    _read_dao = factory


def get_read_dao() -> Any:
    """获取只读查询 DAO 实例（语义同 infrastructure.repositories.read.Information_Read()）。"""
    if _read_dao is None:
        raise _unloaded("只读 DAO", "register_read_dao")
    return _read_dao()


# ---------------------------------------------------------------------------
# 用户账号写入 DAO（user.Information）
# ---------------------------------------------------------------------------

def register_user_dao(factory: Callable[[], Any]) -> None:
    """注册用户账号写入 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _user_dao
    _user_dao = factory


def get_user_dao() -> Any:
    """获取用户账号写入 DAO 实例（语义同 infrastructure.repositories.user.Information()）。"""
    if _user_dao is None:
        raise _unloaded("用户写入 DAO", "register_user_dao")
    return _user_dao()


# ---------------------------------------------------------------------------
# 会话消息写入 DAO（information.Information）
# ---------------------------------------------------------------------------

def register_information_dao(factory: Callable[[], Any]) -> None:
    """注册会话消息写入 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _information_dao
    _information_dao = factory


def get_information_dao() -> Any:
    """获取会话消息写入 DAO 实例（语义同 infrastructure.repositories.information.Information()）。"""
    if _information_dao is None:
        raise _unloaded("消息写入 DAO", "register_information_dao")
    return _information_dao()


# ---------------------------------------------------------------------------
# 会话历史/标题写入 DAO（Information_history）
# ---------------------------------------------------------------------------

def register_history_dao(factory: Callable[[], Any]) -> None:
    """注册会话历史写入 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _history_dao
    _history_dao = factory


def get_history_dao() -> Any:
    """获取会话历史写入 DAO 实例（语义同 infrastructure.repositories.history.Information_history()）。"""
    if _history_dao is None:
        raise _unloaded("历史写入 DAO", "register_history_dao")
    return _history_dao()


# ---------------------------------------------------------------------------
# 用户反馈 DAO（FeedbackDAO）
# ---------------------------------------------------------------------------

def register_feedback_dao(factory: Callable[[], Any]) -> None:
    """注册用户反馈 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _feedback_dao
    _feedback_dao = factory


def get_feedback_dao() -> Any:
    """获取用户反馈 DAO 实例（语义同 infrastructure.repositories.feedback.FeedbackDAO()）。"""
    if _feedback_dao is None:
        raise _unloaded("反馈 DAO", "register_feedback_dao")
    return _feedback_dao()


# ---------------------------------------------------------------------------
# 知识库文档 DAO（KnowledgeDAO）
# ---------------------------------------------------------------------------

def register_knowledge_dao(factory: Callable[[], Any]) -> None:
    """注册知识库文档 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。

    参数：factory —— 零参工厂；实现 KnowledgeDAO 构造需 upload_dir，
          组合根以闭包注入 settings.UPLOAD_DIR。
    """
    global _knowledge_dao
    _knowledge_dao = factory


def get_knowledge_dao() -> Any:
    """获取知识库文档 DAO 实例（语义同 KnowledgeDAO(settings.UPLOAD_DIR)）。"""
    if _knowledge_dao is None:
        raise _unloaded("知识库 DAO", "register_knowledge_dao")
    return _knowledge_dao()


# ---------------------------------------------------------------------------
# Agent 链路日志 DAO（ChainLogDAO）
# ---------------------------------------------------------------------------

def register_chain_log_dao(factory: Callable[[], Any]) -> None:
    """注册链路日志 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _chain_log_dao
    _chain_log_dao = factory


def get_chain_log_dao() -> Any:
    """获取链路日志 DAO 实例（语义同 infrastructure.repositories.chain_log.ChainLogDAO()）。"""
    if _chain_log_dao is None:
        raise _unloaded("链路日志 DAO", "register_chain_log_dao")
    return _chain_log_dao()


# ---------------------------------------------------------------------------
# 用户画像 DAO（ProfileDAO）
# ---------------------------------------------------------------------------

def register_profile_dao(factory: Callable[[], Any]) -> None:
    """注册用户画像 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _profile_dao
    _profile_dao = factory


def get_profile_dao() -> Any:
    """获取用户画像 DAO 实例（语义同 infrastructure.repositories.profile.ProfileDAO()）。"""
    if _profile_dao is None:
        raise _unloaded("画像 DAO", "register_profile_dao")
    return _profile_dao()


# ---------------------------------------------------------------------------
# 会话关键词 DAO（SessionKeywordDAO）
# ---------------------------------------------------------------------------

def register_session_keyword_dao(factory: Callable[[], Any]) -> None:
    """注册会话关键词 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _session_keyword_dao
    _session_keyword_dao = factory


def get_session_keyword_dao() -> Any:
    """获取会话关键词 DAO 实例（语义同 infrastructure.repositories.session_keyword.SessionKeywordDAO()）。"""
    if _session_keyword_dao is None:
        raise _unloaded("会话关键词 DAO", "register_session_keyword_dao")
    return _session_keyword_dao()


# ---------------------------------------------------------------------------
# 文档审核 DAO（DocumentReviewDAO）
# ---------------------------------------------------------------------------

def register_document_review_dao(factory: Callable[[], Any]) -> None:
    """注册文档审核 DAO 工厂（组合根启动时调用一次；测试可注入假实现）。"""
    global _document_review_dao
    _document_review_dao = factory


def get_document_review_dao() -> Any:
    """获取文档审核 DAO 实例（语义同 infrastructure.repositories.document_review.DocumentReviewDAO()）。"""
    if _document_review_dao is None:
        raise _unloaded("文档审核 DAO", "register_document_review_dao")
    return _document_review_dao()


# ---------------------------------------------------------------------------
# 上传文件落盘命名（knowledge.build_stored_filename）
# ---------------------------------------------------------------------------

def register_build_stored_filename(fn: Callable[..., str]) -> None:
    """注册落盘文件名构造函数（组合根启动时调用一次）。"""
    global _build_stored_filename
    _build_stored_filename = fn


def build_stored_filename(user_id: int, original_name: str, ext: str) -> str:
    """构造上传文件落盘名（签名与行为同 infrastructure.repositories.knowledge 实现）。"""
    if _build_stored_filename is None:
        raise _unloaded("落盘命名", "register_build_stored_filename")
    return _build_stored_filename(user_id, original_name, ext)


# ---------------------------------------------------------------------------
# 撤销最近一次软删除（soft_delete.recover_last_deleted）
# ---------------------------------------------------------------------------

def register_recover_last_deleted(fn: Callable[[int], Optional[str]]) -> None:
    """注册软删除恢复函数（组合根启动时调用一次）。"""
    global _recover_last_deleted
    _recover_last_deleted = fn


def recover_last_deleted(user_id: int) -> Optional[str]:
    """撤销当前用户最近一次软删除（签名与行为同 infrastructure.repositories.soft_delete 实现）。"""
    if _recover_last_deleted is None:
        raise _unloaded("软删除恢复", "register_recover_last_deleted")
    return _recover_last_deleted(user_id)


# ---------------------------------------------------------------------------
# ORM 事务作用域（persistence.session.session_scope）
# ---------------------------------------------------------------------------

def register_session_scope(fn: Callable[[], Any]) -> None:
    """注册 ORM 事务作用域工厂（组合根启动时调用一次）。"""
    global _session_scope
    _session_scope = fn


def session_scope() -> Any:
    """获取 ORM 事务作用域上下文管理器（语义同 infrastructure.persistence.session.session_scope）。"""
    if _session_scope is None:
        raise _unloaded("事务作用域", "register_session_scope")
    return _session_scope()
