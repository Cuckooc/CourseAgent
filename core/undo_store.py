"""
模块名：core.undo_store（撤销恢复存储）。

作用：
    每次软删除操作自动暂存一条撤销记录，给用户一次“反悔恢复”的机会。
    每用户仅保留最近一条记录（新记录覆盖旧记录）；consume 取出后立即清空，
    即每次软删除赠送一次恢复机会，用完即止；过期记录由 purge_scheduler 的
    软删除保留期清理负责（本模块不设 TTL）。

存储策略（Redis 优先 + 进程内存回退）：
    Redis 可用时写入键 undo:<user_id>（多副本共享）；Redis 不可用或读写
    异常时降级到模块级 _memory_store（加锁的进程内字典，单机语义）。

主要成员：
    UndoStore（全静态/类方法工具类，无需实例化）：
    save() 暂存、consume() 取出并删除、peek() 只读查看。

被谁使用（Grep UndoStore）：
    - dao/soft_delete.py：soft_delete_session、soft_delete_user 成功后
      调用 UndoStore.save 暂存被删对象定位信息；
    - control/history_control.py：撤销恢复端点调用 UndoStore.consume
      取出最近一次删除记录后执行恢复。
    peek() 当前全仓无调用方（预留给“查询是否可撤销”类场景）。
"""
import json
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 内存降级存储及其互斥锁：str(user_id) -> 撤销记录 payload
_lock = threading.Lock()
_memory_store: Dict[str, Dict[str, Any]] = {}


class UndoStore:
    """撤销记录存储（Redis 优先，失败回退进程内存）。

    作用：对上层屏蔽存储介质差异，提供 save/consume/peek 三个语义一致的
    类方法；全部为 @classmethod/@staticmethod，不实例化，调用方以
    UndoStore.save(...) 形式直接使用（实例化位置：无）。
    记录结构：{"table": 业务对象类型, "pk": 主键字典, "deleted_at": 删除时间}。
    """

    @staticmethod
    def _key(user_id: int) -> str:
        """生成某用户撤销记录在 Redis 中的键名 undo:<user_id>。"""
        return "undo:{}".format(user_id)

    @classmethod
    def save(cls, user_id: int, table: str, pk: dict, deleted_at: str) -> None:
        """暂存一条撤销记录（覆盖该用户之前的记录）。

        功能：把被软删对象的类型、主键与删除时间序列化为 JSON，
        Redis 可用则 SET 覆盖写；失败或未配置则写入进程内字典。
        被谁调用：dao/soft_delete.py 的 soft_delete_session
            （table="session"）、soft_delete_user（table="user"），
            均在软删 SQL 执行成功后调用。
        参数：
            user_id: 执行删除的用户 id（HTTP 鉴权身份，经 service 透传）；
            table: 被删对象类型（"session"/"user"），决定恢复时走哪条链路；
            pk: 主键定位字典，如 {"session_id": id} 或 {"user_id": id}，
                恢复时据此找回目标行；
            deleted_at: 删除时间戳字符串（str(time.time())）。
        返回：None。
        """
        payload = {
            "table": table,
            "pk": pk,
            "deleted_at": deleted_at,
        }
        # 延迟 import 避免潜在的模块循环依赖
        from app.infrastructure.redis.redis_client import get_redis
        r = get_redis()
        if r is not None:
            try:
                # SET 天然“新覆盖旧”：每用户只保留最近一次删除的撤销机会
                r.set(cls._key(user_id), json.dumps(payload))
                return
            except Exception as e:
                # Redis 写失败不阻断删除主流程，降级内存继续暂存
                logger.warning("UndoStore Redis save failed, falling back to memory: %s", e)
        with _lock:
            _memory_store[str(user_id)] = payload

    @classmethod
    def consume(cls, user_id: int) -> Optional[Dict[str, Any]]:
        """取出并删除撤销记录（一次性消费）。

        功能：读取用户最近一条撤销记录并立即删除，保证恢复机会只能使用一次。
        被谁调用：control/history_control.py 的撤销恢复端点，
            取到记录后执行反软删（is_deleted 置回 0）。
        参数：
            user_id: 当前登录用户 id（来自 get_current_user 鉴权结果）。
        返回：Optional[Dict[str, Any]]；有记录返回
            {"table","pk","deleted_at"} 字典供恢复逻辑使用；
            无记录返回 None（端点转为“没有可撤销的删除记录”错误）。
        """
        from app.infrastructure.redis.redis_client import get_redis
        r = get_redis()
        payload = None
        if r is not None:
            try:
                raw = r.get(cls._key(user_id))
                if raw:
                    payload = json.loads(raw)
                    # 先读后删实现一次性消费（成功路径下读与删紧邻执行）
                    r.delete(cls._key(user_id))
                    return payload
            except Exception as e:
                # Redis 读异常时不直接判空，继续尝试内存口径
                logger.warning("UndoStore Redis consume failed: %s", e)
        with _lock:
            # pop 即“取出并删除”，与 Redis 路径的一次性语义保持一致
            payload = _memory_store.pop(str(user_id), None)
        return payload

    @classmethod
    def peek(cls, user_id: int) -> Optional[Dict[str, Any]]:
        """查看撤销记录但不消费（不删除，可重复读取）。

        被谁调用：当前全仓 Grep 无调用方，预留给“前端展示是否可撤销”等场景。
        参数：
            user_id: 用户 id（鉴权身份）。
        返回：Optional[Dict[str, Any]]；有记录返回其副本（内存路径返回拷贝，
            防止调用方误改内部数据），无记录返回 None。
        """
        from app.infrastructure.redis.redis_client import get_redis
        r = get_redis()
        if r is not None:
            try:
                raw = r.get(cls._key(user_id))
                if raw:
                    return json.loads(raw)
            except Exception as e:
                logger.warning("UndoStore Redis peek failed: %s", e)
        with _lock:
            data = _memory_store.get(str(user_id))
            if data is not None:
                # 返回浅拷贝，隔离调用方对内存中原始记录的修改
                return dict(data)
        return None
