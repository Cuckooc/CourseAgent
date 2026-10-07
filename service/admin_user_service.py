"""
模块名：service.admin_user_service
作用：管理端用户管理服务，提供用户列表查询、角色调整与账号注销（软删除 +
      Redis 残留键清理），是 control/admin_control.py 与 DAO 层之间的业务编排层。

注销语义为软删除 + 7 天冷静期：
- MySQL 软删除：会话 / 历史 / 画像 / 账号标记 is_deleted=1（dao.soft_delete）；
- 注销计划：写入 account_deletion_schedule，7 天后由定时任务硬删除；
- Redis：短期记忆、上下文记忆、画像暂存与节流、入库到期队列、按用户用量、
  会话号序列锁。注销后该用户未过期 token 由 get_current_user 回库校验立即失效。

主要成员：
- list_all_users()：查询全部用户基础信息（不含密码哈希）。
- update_role(user_id, role)：调整用户角色（user/teacher）。
- deactivate_user(user_id)：注销用户（MySQL 软删除 + 注销计划 + Redis 清理）。
- _REDIS_PATTERNS：注销时需 SCAN 清理的按用户 Redis 键模式（模块级常量）。

被谁使用：
- control/admin_control.py：以 `from service import admin_user_service` 导入，
  分别在用户列表、角色调整、注销确认（deactivate_user_confirm）接口中调用
  上述三个函数，结果经统一响应封装返回管理端前端。
"""
import logging

from app.infrastructure.persistence.repositories.user import Information
from app.infrastructure.persistence.repositories.read import Information_Read

# 模块级日志器：注销/角色变更等管理动作的审计日志统一走该 logger
logger = logging.getLogger(__name__)

# 按用户 ID 的 Redis 键模式（含会话级子键，用 SCAN 清理）
# 含义：注销用户时需要逐模式 SCAN 并删除的全部残留键；{uid} 占位在使用时
# format 为目标用户 ID。各子键分别对应短期记忆、上下文记忆、画像暂存/节流、
# 月度 token 用量与会话号发号锁（键的写入方分布在 memory/ 与 core/usage.py）。
_REDIS_PATTERNS = (
    "mem:short:{uid}:*",       # 短期记忆（含 :meta）
    "mem:ctx:{uid}:*",         # 上下文记忆
    "mem:profile:pending:{uid}",  # 画像 7 天暂存
    "mem:profile:ext:{uid}",   # 画像提取节流
    "llm:user_usage:{uid}:*",  # 月度 token 用量
    "seq:{uid}",               # 会话号发号锁
)


def list_all_users() -> list:
    """查询全部用户的基础信息。

    功能：透传只读 DAO 查询全量用户列表，供管理端用户管理页展示。
    被谁调用：control/admin_control.py 的用户列表接口（list_users）。
    参数：无。
    返回：list——元素为 dao/read.py 的 Information_Read.get_all_users()
          返回的用户记录字典（不含密码哈希字段）；去向：经 control 层
          success(users=...) 封装为 JSON 返回管理端前端。
    """
    # 数据来源：dao/read.py 的 Information_Read（只读查询 user_information 表）
    return Information_Read().get_all_users()


def update_role(user_id: int, role: str) -> bool:
    """调整指定用户的角色。

    功能：将目标用户角色更新为 user/teacher（admin 角色由运维侧管理，
          不走本接口）；成功时写一条审计日志。
    被谁调用：control/admin_control.py 的角色调整接口（update_user_role），
              user_id/role 来自管理端请求体（接口侧已做管理员鉴权）。
    参数：
    - user_id (int)：目标用户 ID，来源：管理端请求体（经 JWT 鉴权后的管理员提交）。
    - role (str)：新角色，来源：管理端请求体，仅允许 user/teacher。
    返回：bool——True 表示 UPDATE 命中行（角色已变更）；False 表示用户不存在
          或数据库异常。去向：control 层据真假返回成功/失败响应。角色即时生效，
          该用户下一个请求经 get_current_user 回库读到新角色。
    """
    # 数据去向：dao/user.py 的 Information.update_role → UPDATE user_information
    ok = Information().update_role(user_id, role)
    if ok:
        logger.info("admin updated role: user_id=%s role=%s", user_id, role)
    return ok


def deactivate_user(user_id: int) -> bool:
    """注销用户：软删除业务数据 + 写入注销计划 + Redis 残留键清理。

    功能：编排账号注销全流程——先由 DAO 完成 MySQL 软删除与 7 天硬删除计划
          写入，再 SCAN 清理该用户在 Redis 中的全部残留键（记忆/画像/用量/
          序列锁）及画像到期队列成员。
    被谁调用：control/admin_control.py 的 deactivate_user_confirm（管理员二次
              确认凭校验通过后调用）。
    参数：
    - user_id (int)：待注销用户 ID，来源：管理端注销确认请求（经
      PendingDeleteStore.verify_token 校验的一次性确认令牌）。
    返回：bool——True 表示账号已软删除且 Redis 已清理；False 表示 DAO 层
          软删除失败（此时不清理 Redis，直接放弃）。
    异常：Redis 不可用时 get_redis() 返回 None，静默跳过缓存清理，
          不影响 MySQL 注销主流程（残留键随 TTL 自然过期）。
    """
    # 延迟导入：Redis 客户端按请求获取，避免模块导入期强依赖 Redis 可用性
    from app.infrastructure.redis.redis_client import get_redis

    # 数据去向：dao/user.py 的 Information.deactivate_user
    # → dao/soft_delete.py 软删除会话/历史/画像/账号 + 写 account_deletion_schedule
    ok = Information().deactivate_user(user_id)
    if not ok:
        return False

    # Redis 清理为注销的附加步骤：缓存层不可用不得让整个注销接口失败
    r = get_redis()
    if r is not None:
        # SCAN 游标式遍历（count=200/批），避免 KEYS 阻塞 Redis 主线程
        for pattern in _REDIS_PATTERNS:
            keys = list(r.scan_iter(match=pattern.format(uid=user_id), count=200))
            if keys:
                r.delete(*keys)
        # 画像入库到期有序集合的成员不在通配模式内，单独按值移除
        r.zrem("mem:profile:due", str(user_id))
    logger.info("admin deactivated user_id=%s (mysql + redis cleaned)", user_id)
    return True
