"""
模块名：app.auth.delete_guard

作用：
    删除操作二次确认。所有高危 DELETE 操作采用「预览 → 确认」两步：
    先调用 preview 类接口获取 confirm_token，再携带 token 调用 confirm 执行实际删除。
    令牌一次性有效、5 分钟过期；存储优先用 Redis（多副本共享），Redis 不可用时回退进程内存。

主要成员：
    - PendingDeleteStore：确认令牌存储类（全部为 @staticmethod，无需实例化，直接类名调用）；
    - _TOKEN_TTL_SECONDS：模块级常量，令牌有效期（300 秒）；
    - _lock：模块级线程锁，保护内存回退字典的并发读写；
    - _memory_store：模块级全局单例，Redis 不可用时的进程内令牌字典（不跨副本/重启）。

被谁使用：
    - app/api/v1/knowledge.py：文档删除 preview/confirm（action=delete_document）；
    - app/api/v1/history.py：会话删除 preview/confirm（action=delete_session）；
    - app/api/v1/admin.py：管理员停用用户 preview/confirm（action=deactivate_user）。
"""
import logging
import secrets
import threading
from typing import Any, Dict, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# 模块级常量：确认令牌有效期（秒），固定 5 分钟，到期 Redis/内存键失效即无法确认。
_TOKEN_TTL_SECONDS = 300
# 模块级全局单例：保护 _memory_store 的线程锁（内存回退分支才有竞争，Redis 分支不需要）。
_lock = threading.Lock()
# 模块级全局单例：Redis 不可用时的进程内令牌存储 {token: payload}。
# 仅在单副本/开发态兜底；多副本下未配置 Redis 时令牌不跨进程，且重启即丢失。
_memory_store: Dict[str, Dict[str, Any]] = {}


class PendingDeleteStore:
    """Redis + 内存回退的删除确认令牌存储。

    类作用：为高危删除操作颁发一次性确认令牌，并在 confirm 阶段校验令牌归属
    （user_id）、动作（action）与有效期，防止误删/CSRF/越权删除。
    实例化位置：全仓均以静态方式调用（PendingDeleteStore.create_token / verify_token），
    不创建实例；调用方为 knowledge_control / history_control / admin_control 的
    preview 与 confirm 端点。
    关键属性去向：本类无实例属性；载荷 payload 含 user_id（令牌归属）、action（删除动作）、
    target_info（删除目标快照），校验通过后 target_info 回传给 confirm 端点执行删除。
    """

    @staticmethod
    def create_token(user_id, action, target_info):
        # type: (int, str, Dict[str, Any]) -> str
        """生成确认令牌，返回 token 字符串。

        功能：生成 24 字节 URL 安全随机令牌，把 {user_id, action, target_info} 载荷
        优先写入 Redis（SETEX，键 del_confirm:<token>，TTL 5 分钟）；
        Redis 不可用/写入异常时加锁写入进程内 _memory_store 兜底。
        被谁调用：app/api/v1/knowledge.py、history_control.py、admin_control.py
                  的删除 preview 端点（先于真正删除）。
        参数：
            user_id: 发起删除的用户 id，来源为 app.auth.guards.get_current_user 解析结果；
            action: 删除动作标识（delete_document/delete_session/deactivate_user），调用方约定；
            target_info: 删除目标快照 dict（如文档 id/会话 id/被停用用户信息），
                         来源为 HTTP 请求参数经 preview 组装，verify 时原样回传。
        返回：str，confirm_token（去向：preview 接口响应体，由前端在 confirm 请求中带回）。
        """
        token = secrets.token_urlsafe(24)
        payload = {
            "user_id": user_id,
            "action": action,
            "target_info": target_info,
        }

        from app.infrastructure.redis.redis_client import get_redis
        r = get_redis()
        if r is not None:
            try:
                import json
                # Redis 正常：多副本共享令牌，SETEX 原子写入并绑定 5 分钟 TTL
                r.setex(
                    "del_confirm:{}".format(token),
                    _TOKEN_TTL_SECONDS,
                    json.dumps(payload),
                )
                return token
            except Exception as e:
                # Redis 降级：写入失败不阻断预览，回退到进程内存存储
                logger.warning("Redis setex failed, falling back to memory: %s", e)

        # 内存回退分支：加锁保证多线程下字典写入安全
        with _lock:
            _memory_store[token] = payload
        return token

    @staticmethod
    def verify_token(token, user_id, action):
        # type: (str, int, str) -> Optional[Dict[str, Any]]
        """验证令牌，返回 target_info；不匹配/过期则返回 None。

        功能：先查 Redis（命中即删除，保证令牌一次性），未命中再在内存字典 pop；
        随后核对载荷中的 user_id 与 action 是否与本次请求一致（防越权/防动作串用）。
        被谁调用：app/api/v1/knowledge.py、history_control.py、admin_control.py
                  的删除 confirm 端点（真正执行删除前）。
        参数：
            token: 前端回传的 confirm_token，来源为 HTTP confirm 请求体；
            user_id: 当前登录用户 id，来源为 app.auth.guards.get_current_user（不信任请求体）；
            action: 本次确认的动作标识，来源为 confirm 端点按路由约定的常量。
        返回：
            Optional[Dict[str, Any]]：校验通过返回载荷中的 target_info（删除目标快照，
            交给后续删除逻辑）；令牌不存在/已过期/已使用/user_id 或 action 不匹配时返回 None
            （调用方据此返回确认失败，不执行删除）。
        """
        from app.infrastructure.redis.redis_client import get_redis
        r = get_redis()
        payload = None

        if r is not None:
            try:
                import json
                # Redis 分支：GET 命中立即 DELETE，令牌只可消费一次（重放/重复确认失效）
                raw = r.get("del_confirm:{}".format(token))
                if raw:
                    payload = json.loads(raw)
                    r.delete("del_confirm:{}".format(token))
            except Exception as e:
                logger.warning("Redis get/del failed: %s", e)

        if payload is None:
            # Redis 未命中（不可用/异常/键过期）时回退进程内存：pop 同样保证一次性
            with _lock:
                payload = _memory_store.pop(token, None)

        if payload is None:
            return None

        if payload.get("user_id") != user_id:
            logger.warning(
                "delete confirm token user_id mismatch: expected=%s, got=%s",
                payload.get("user_id"), user_id,
            )
            return None

        if payload.get("action") != action:
            logger.warning(
                "delete confirm token action mismatch: expected=%s, got=%s",
                payload.get("action"), action,
            )
            return None

        return payload.get("target_info")
