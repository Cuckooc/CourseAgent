"""
模块：app.domain.memory.profile_service —— 用户画像服务（用户级长期记忆：习惯/兴趣/常问主题）。

作用：维护每用户的画像（profile_text/interests/topics 三字段），对话期间
异步从对话中提取画像信号，读取时渲染为【用户画像】前缀注入 LLM prompt
（经 app/application/chat/chat_service → AgentService → app/domain/agents/ChatAgent 的
user_profile 形参）。数据链路：自动提取/手动编辑 → Redis pending 暂存
（连续 N 天无更新）→ 惰性/后台 flush → dao/profile.ProfileDAO.upsert 落
MySQL user_profile 表；读取 = MySQL 基线 + pending 覆盖。画像为 user 级
存储，天然跨会话，会话滚换（app/domain/memory/session_rollover）无需迁移画像。

数据来源：
1. 自动提取：每轮对话结束后异步从"用户问题 + 助手回答"中提取画像信号，
   与现有画像合并（节流：同用户 settings.PROFILE_EXTRACT_INTERVAL_SECONDS
   （默认 600 秒）内最多一次）；
2. 手动维护：个人信息页可随时编辑；未修改则沿用原画像。

写入策略（产品约定：变更先暂存 Redis，连续 settings.PROFILE_PENDING_TTL_DAYS
（默认 7）天没有更新才落 MySQL）：
- pending hash  mem:profile:pending:{uid}  字段 profile_text/interests/topics
- 到期索引      mem:profile:due            zset member=uid score=应落库时间戳
- 每次更新（自动/手动）都覆盖 pending 并把 score 重置为 now+7d（重新计时）；
- flush 时机：读取时惰性 flush 本人 + 后台守护线程定期扫 due（默认 1800 秒
  周期，当前为 start_background_flusher 形参默认值；进程重启后启动钩子也会
  补扫一次），flush 成功才删 pending 与 zset 成员；
- 画像读取 = MySQL 基线 + pending 覆盖（pending 存在时以 pending 为准）。

主要成员：
- ProfileService：画像服务类（Redis pending/due/throttle + 线程池异步提取
  + 后台落库线程，含进程内 dict 降级；MySQL 经 dao/profile.ProfileDAO）；
- render_profile_prefix()：把画像 dict 渲染为 LLM 注入前缀（模块级函数）；
- get_profile_service()：应用级单例工厂（@lru_cache）；
- reset_profile_service_for_test()：测试辅助，清空单例缓存；
- _parse_profile_llm()：解析模型三行格式输出；_EXTRACT_PROMPT：提取提示词。

被谁使用（全仓 import 位置）：
- app/application/chat/chat_service.py：ChatService.__init__ 持单例；_get_profile_prefix
  每轮调 get_profile + render_profile_prefix 注入；_save_information 每轮
  调 extract_from_conversation_async 异步提取；
- app/api/v1/profile.py：GET/PUT /profile 调 get_profile/update_profile；
- control/app.py 的 lifespan 启动钩子：start_background_flusher 启守护线程。
"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Dict, Optional

from core.config import settings
from app.application.ports.llm import build_chat_model
from app.infrastructure.redis.locks import try_acquire_cycle_lock
from app.infrastructure.redis.redis_client import get_redis
from app.infrastructure.persistence.repositories.profile import ProfileDAO

logger = logging.getLogger(__name__)

# 模块级常量：Redis pending hash 前缀，完整键 mem:profile:pending:{user_id}
_PENDING_PREFIX = "mem:profile:pending:"
# 模块级常量：到期落库索引 zset 的全局键，member=uid（字符串），score=应落库时间戳
_DUE_KEY = "mem:profile:due"
# 模块级常量：自动提取节流键前缀，完整键 mem:profile:ext:{user_id}（SET NX EX）
_THROTTLE_PREFIX = "mem:profile:ext:"

# 多 worker/多副本部署时各进程都会启动本 flusher，用 Redis 周期锁互斥
# （见 core.locks.try_acquire_cycle_lock），避免并发落库/删暂存。
_PROFILE_FLUSH_LOCK_KEY = "flusher:profile"

# 模块级常量：字段长度保护上限——profile_text 最长 4000 字、
# interests/topics 各 500 字（超长截断，防止异常模型输出撑爆 MySQL 字段）
_PROFILE_TEXT_MAX = 4000
_FIELD_MAX = 500

# 模块级常量：画像提取提示词。import 时即定义（纯字符串）；
# 占位符 {current}/{user_text}/{ai_text} 由 _do_extract 填充；
# 约定模型只输出【画像】【兴趣】【主题】三行，由 _parse_profile_llm 解析。
_EXTRACT_PROMPT = """你是用户画像分析助手。请根据【现有画像】与【本轮对话】更新该用户的画像。
只输出以下三行（不要输出其他内容，没有内容的行留空）：
【画像】<分条要点，覆盖学习/工作领域、表达习惯、关注重点，总字数不超过200字，用"；"分隔>
【兴趣】<兴趣爱好，用顿号分隔，不超过100字>
【主题】<经常提问的主题/方向，用顿号分隔，不超过100字>
要求：合并去重，保留现有画像中仍然有效的信息，被本轮对话否定的信息删除或更新；
本轮对话没有新增信息时原样返回现有画像。

现有画像：
{current}

本轮对话：
用户：{user_text}
助手：{ai_text}
"""

# 模块级常量：pending hash 的三个业务字段名（读取/落库按此顺序映射）
_FIELDS = ("profile_text", "interests", "topics")


def _pending_key(user_id: int) -> str:
    """拼接 pending hash 键：mem:profile:pending:{user_id}。

    参数：user_id (int)——JWT 注入的用户 ID。返回：str——Redis 键。
    """
    return f"{_PENDING_PREFIX}{user_id}"


def _throttle_key(user_id: int) -> str:
    """拼接自动提取节流键：mem:profile:ext:{user_id}。

    参数：user_id (int)——JWT 注入的用户 ID。返回：str——Redis 键。
    """
    return f"{_THROTTLE_PREFIX}{user_id}"


def _parse_profile_llm(text: str) -> Dict[str, str]:
    """解析 LLM 的三行格式；解析失败时把整体作为 profile_text。

    被谁调用：_invoke_llm。
    参数：text (str)——模型原始输出（【画像】…/【兴趣】…/【主题】…三行）。
    返回：Dict[str,str]——含 profile_text/interests/topics 三键；
    一行标签都未识别时把整段去空白文本塞进 profile_text（容错降级，
    避免模型偶发不遵循格式导致画像更新完全丢失）。
    """
    out = {"profile_text": "", "interests": "", "topics": ""}
    label_map = {"画像": "profile_text", "兴趣": "interests", "主题": "topics"}
    matched = False
    for line in (text or "").splitlines():
        m = re.match(r"^\s*【(画像|兴趣|主题)】\s*(.*)$", line.strip())
        if m:
            out[label_map[m.group(1)]] = m.group(2).strip()
            matched = True
    if not matched:
        out["profile_text"] = (text or "").strip()
    return out


def render_profile_prefix(profile: Optional[Dict[str, Any]]) -> str:
    """生成注入模型输入的画像前缀；画像为空返回空串。

    被谁调用：app/application/chat/chat_service.py 的 ChatService._get_profile_prefix
    （每轮对话渲染后随 prompt 注入 app/domain/agents/ChatAgent 的 user_profile）。
    参数：profile (dict|None)——get_profile 返回的画像 dict
    （来源：MySQL user_profile 基线 + Redis pending 覆盖）。
    返回：str——"【用户画像】\\n…"注入文本；三字段全空时返回 ""，
    调用方按“无画像”处理，不影响对话主链路。
    """
    if not profile:
        return ""
    lines = []
    text = (profile.get("profile_text") or "").strip()
    if text:
        lines.append(text)
    interests = (profile.get("interests") or "").strip()
    topics = (profile.get("topics") or "").strip()
    if interests:
        lines.append(f"兴趣爱好：{interests}")
    if topics:
        lines.append(f"常问主题：{topics}")
    if not lines:
        return ""
    return "【用户画像】\n" + "\n".join(lines)


class ProfileService:
    """
    用户画像服务（应用级单例，经 get_profile_service 获取；
    chat_service、profile_control、app.py lifespan 共用同一实例）。

    存储与策略：
    - Redis 可用：pending hash（变更暂存）+ due zset（到期落库索引）+
      ext 节流键；pending 同时设置 7 天 +1 天的保底 TTL 防脏数据永驻；
    - 落库目标：MySQL user_profile（dao/profile.ProfileDAO.upsert/get），
      延迟天数 settings.PROFILE_PENDING_TTL_DAYS（默认 7），自动提取节流
      settings.PROFILE_EXTRACT_INTERVAL_SECONDS（默认 600 秒）；
    - Redis 不可用：降级为进程内 dict（_mem_pending/_mem_due/_mem_throttle，
      语义与 Redis 一致但仅单机有效）；
    - 自动提取在 2 线程的 ThreadPoolExecutor 内异步执行，不阻塞对话链路。

    实例化位置：生产仅由模块底部 get_profile_service()（@lru_cache）无参
    构造；client/profile_dao 形参保留给测试注入。
    """

    def __init__(self, client=None, profile_dao: ProfileDAO = None):
        """
        形参（生产由单例工厂无参构造，以下仅测试注入用）：
        - client：Redis 客户端，None 时由 client 属性惰性取
          core.redis_client.get_redis() 全局连接；
        - profile_dao：画像 DAO（dao/profile.ProfileDAO，读写 MySQL
          user_profile），None 时自行 new ProfileDAO()。
        关键属性去向：_pool 承载异步提取任务；_mem_pending/_mem_due/
        _mem_throttle + _lock 为 Redis 不可用时的进程内降级；
        _flusher_started 保证后台落库线程幂等启动一次。
        """
        self._client = client
        self._dao = profile_dao or ProfileDAO()
        # 2 线程小池：仅跑画像提取 LLM 调用，避免与对话主链路争抢资源
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="profile")
        # Redis 不可用时的进程内降级（与 Redis 语义一致，仅单机有效）
        self._mem_pending: Dict[int, Dict[str, Any]] = {}
        self._mem_due: Dict[int, float] = {}
        self._mem_throttle: Dict[int, float] = {}
        self._lock = threading.Lock()
        self._flusher_started = False

    @property
    def client(self):
        """惰性获取 Redis 客户端：注入实例优先，否则取全局 get_redis()；
        返回 None 表示 Redis 不可用，各方法据此走进程内 dict 降级分支。"""
        if self._client is not None:
            return self._client
        return get_redis()

    # ============================================================ 读取

    def get_profile(self, user_id: int) -> Dict[str, Any]:
        """读取画像：先惰性 flush 本人到期暂存，再 MySQL 基线 + pending 覆盖。

        被谁调用：app/api/v1/profile.py 的 GET /profile（返回前端）；
        app/application/chat/chat_service.py 的 _get_profile_prefix（每轮注入 LLM）；
        _do_extract（合并前读现有画像）；update_profile 写完回读。
        参数：user_id (int)——JWT 注入的用户 ID。
        返回：Dict[str,Any]——{user_id, profile_text, interests, topics,
        pending, due_at}；pending=True 表示当前值来自 Redis 暂存（尚未落
        MySQL），due_at 为应落库时间戳；pending=False 表示值来自 MySQL
        基线，due_at=None。user_id 为空返回 _empty 空画像。
        异常：DAO/Redis 异常由下层方法各自兜底（pending 读失败按无暂存）。
        """
        if not user_id:
            return self._empty(user_id)
        self._flush_if_due(user_id)
        base = self._dao.get(user_id) or {}
        pending = self._read_pending(user_id)
        if pending is not None:
            merged = {
                "user_id": user_id,
                "profile_text": pending.get("profile_text", ""),
                "interests": pending.get("interests", ""),
                "topics": pending.get("topics", ""),
                "pending": True,
                "due_at": self._due_at(user_id),
            }
        else:
            merged = {
                "user_id": user_id,
                "profile_text": base.get("profile_text", "") or "",
                "interests": base.get("interests", "") or "",
                "topics": base.get("topics", "") or "",
                "pending": False,
                "due_at": None,
            }
        return merged

    # ============================================================ 手动更新

    def update_profile(
        self,
        user_id: int,
        profile_text: str = "",
        interests: str = "",
        topics: str = "",
    ) -> Dict[str, Any]:
        """手动编辑画像 → 暂存 Redis 并重新计时 7 天。

        被谁调用：app/api/v1/profile.py 的 PUT /profile
        （个人信息页保存，参数来自 ProfileUpdateRequest 请求体）。
        参数：user_id (int，JWT)；profile_text/interests/topics (str)——
        用户提交的三字段，去空白并按 _PROFILE_TEXT_MAX/_FIELD_MAX 截断。
        返回：Dict[str,Any]——回读的最新画像（结构同 get_profile，
        pending=True），去向：/profile JSON 响应返回 control/前端。
        注意：手动编辑同样走延迟落库（不直接写 MySQL），连续 7 天无更新才落库。
        """
        payload = {
            "profile_text": (profile_text or "").strip()[:_PROFILE_TEXT_MAX],
            "interests": (interests or "").strip()[:_FIELD_MAX],
            "topics": (topics or "").strip()[:_FIELD_MAX],
        }
        self._write_pending(user_id, payload)
        logger.info("profile manually updated (pending): uid=%s", user_id)
        return self.get_profile(user_id)

    # ============================================================ 自动提取

    def extract_from_conversation_async(self, user_id: int, user_text: str, ai_text: str) -> None:
        """异步从一轮对话提取/合并画像（不阻塞对话落库）。节流见模块说明。

        被谁调用：app/application/chat/chat_service.py 的 ChatService._save_information
        （每轮拿到完整 LLM 回答后投递一次）。
        参数：user_id (int，JWT)；user_text (str)——本轮用户提问；
        ai_text (str)——本轮助手回答（后两者来自对话请求体与 AgentService
        产出，传入工作线程前各截前 2000 字控成本）。
        返回：None（任务提交到 _pool 后立即返回）。
        异常：用户问题为空白/节流窗口内已有任务时直接跳过；线程池提交失败
        理论上由调用方外层 try 兜底，画像提取永不阻断对话链路。
        """
        if not user_id or not (user_text or "").strip():
            return
        if not self._acquire_throttle(user_id):
            return
        self._pool.submit(self._do_extract, user_id, user_text[:2000], (ai_text or "")[:2000])

    def _do_extract(self, user_id: int, user_text: str, ai_text: str) -> None:
        """线程池任务体：读现有画像 → 拼提示词调 LLM → 解析并写回 pending。

        被谁调用：extract_from_conversation_async 提交到 _pool 的工作线程。
        参数：user_id (int)；user_text/ai_text (str)——已截断的本轮问答。
        返回：None。成功且三字段不全空时 _write_pending 暂存 Redis 并
        重新计时 7 天（后台/惰性 flush 落 dao/profile → MySQL）。
        异常：任何异常仅记 error 日志（异步任务无法上抛，也不影响主链路）。
        """
        try:
            current = self.get_profile(user_id)
            current_text = current.get("profile_text", "")
            if current.get("interests"):
                current_text += f"\n兴趣爱好：{current['interests']}"
            if current.get("topics"):
                current_text += f"\n常问主题：{current['topics']}"
            prompt = _EXTRACT_PROMPT.format(
                current=current_text.strip() or "（暂无）",
                user_text=user_text,
                ai_text=ai_text,
            )
            parsed = self._invoke_llm(prompt)
            if not parsed:
                return
            payload = {
                "profile_text": parsed.get("profile_text", "")[:_PROFILE_TEXT_MAX],
                "interests": parsed.get("interests", "")[:_FIELD_MAX],
                "topics": parsed.get("topics", "")[:_FIELD_MAX],
            }
            if not any(payload.values()):
                # 模型认为本轮无新增信息且未回传现有画像：不覆盖暂存
                return
            self._write_pending(user_id, payload)
            logger.info("profile auto-extracted (pending): uid=%s", user_id)
        except Exception as e:
            logger.error("profile extract failed uid=%s: %s", user_id, e)

    def _invoke_llm(self, prompt: str) -> Optional[Dict[str, str]]:
        """调用 LLM 并把输出解析为画像三字段 dict。

        被谁调用：_do_extract。
        参数：prompt (str)——已填充的 _EXTRACT_PROMPT。
        返回：Dict[str,str]|None——解析结果（键 profile_text/interests/
        topics）；LLM 调用异常时返回 None，调用方跳过本轮更新。
        """
        try:
            llm = build_chat_model()
            resp = llm.invoke(prompt)
            content = getattr(resp, "content", "") or str(resp)
            return _parse_profile_llm(content)
        except Exception as e:
            logger.error("profile LLM call failed: %s", e)
            return None

    # ============================================================ 延迟落库

    def flush_due(self, limit: int = 100) -> int:
        """把已到 7 天期限的 pending 画像落 MySQL。返回 flush 用户数。

        被谁调用：后台守护线程 _run（启动补扫一次 + 每周期一次，周期
        start_background_flusher 形参默认 1800 秒）；无其他生产调用方。
        参数：limit (int)——单周期最多处理的用户数（ZRANGEBYSCORE LIMIT，
        默认 100）。返回：int——实际落库成功的用户数。
        数据去向：ProfileDAO.upsert → MySQL user_profile；成功后才删
        pending hash 与 due zset 成员（失败保留，下周期重试）。
        异常：扫描级异常记 error 后返回已完成计数；单用户失败在
        _flush_one 内处理，不影响后续用户。
        """
        now = time.time()
        client = self.client
        flushed = 0
        try:
            if client is not None:
                # score 从负无穷到当前时间：取所有“应落库时间 ≤ now”的到期用户
                uids = client.zrangebyscore(_DUE_KEY, "-inf", now, start=0, num=limit)
            else:
                with self._lock:
                    uids = [uid for uid, due in self._mem_due.items() if due <= now][:limit]
            for raw_uid in uids or []:
                try:
                    uid = int(raw_uid)
                except (TypeError, ValueError):
                    # zset member 脏数据（非数字 uid）：跳过，不中断整批
                    continue
                if self._flush_one(uid):
                    flushed += 1
        except Exception as e:
            logger.error("profile flush_due failed: %s", e)
        if flushed:
            logger.info("profile flush_due persisted %s users", flushed)
        return flushed

    def force_flush_user(self, user_id: int) -> bool:
        """强制把某用户 pending 落库（管理/测试用，忽略 7 天到期时间）。

        被谁调用：当前无生产路由调用，供管理脚本/测试直接触发落库。
        参数：user_id (int)——用户 ID。返回：bool——是否落库成功
        （无 pending 或 DAO 失败返回 False）。
        """
        return self._flush_one(user_id)

    def start_background_flusher(self, interval_seconds: int = 1800) -> None:
        """启动守护线程：定期把到期暂存落库（幂等，仅启动一次）。

        被谁调用：control/app.py 的 lifespan 启动钩子（使用默认周期 1800 秒）。
        参数：interval_seconds (int)——扫描周期秒数，默认 1800（30 分钟；
        注意该默认值是本形参写死的，未走 core/config.py settings）。
        返回：None。
        并发：每周期经 core.locks.try_acquire_cycle_lock 抢 Redis 周期锁，
        多 worker/多副本下每周期只有一个进程扫 due，抢不到跳过；锁异常/
        Redis 不可用时放行。线程为 daemon，主进程退出即结束。
        """
        if self._flusher_started:
            return
        self._flusher_started = True

        def _run():
            # 启动即补扫一次，处理进程停机期间到期的数据
            self.flush_due()
            while True:
                time.sleep(interval_seconds)
                try:
                    if not try_acquire_cycle_lock(_PROFILE_FLUSH_LOCK_KEY, interval_seconds):
                        continue  # 其他 worker 正在本周期落库，跳过
                    self.flush_due()
                except Exception as e:
                    # 线程级兜底：单周期异常不杀死守护线程
                    logger.error("profile background flush error: %s", e)

        t = threading.Thread(target=_run, name="profile-flusher", daemon=True)
        t.start()

    def _flush_if_due(self, user_id: int) -> None:
        """读取侧惰性落库：本人 pending 已到 due 时间则立即 flush。

        被谁调用：get_profile（每次读画像先检查）。
        参数：user_id (int)。返回：None；无 due 记录或未到期时不做任何事。
        """
        due = self._due_at(user_id)
        if due is not None and due <= time.time():
            self._flush_one(user_id)

    def _flush_one(self, user_id: int) -> bool:
        """把单个用户的 pending 画像落 MySQL，成功后清理暂存与到期索引。

        被谁调用：flush_due（批量）、force_flush_user（强制）、
        _flush_if_due（读取惰性）。
        参数：user_id (int)。返回：bool——True 落库成功并已清理暂存；
        False 表示无 pending（顺带移除残留 due 成员）或 DAO 写失败
        （保留 pending/due，下周期重试）。
        数据去向：ProfileDAO.upsert → MySQL user_profile 表。
        """
        pending = self._read_pending(user_id)
        if pending is None:
            # 到期索引还在但暂存已丢失（如保底 TTL 过期）：清理脏索引
            self._remove_due(user_id)
            return False
        ok = self._dao.upsert(
            user_id,
            pending.get("profile_text", ""),
            pending.get("interests", ""),
            pending.get("topics", ""),
        )
        if not ok:
            return False
        # 先落库成功再删暂存：失败任一步都保留 Redis 数据等待重试，不丢更新
        self._delete_pending(user_id)
        self._remove_due(user_id)
        logger.info("profile pending flushed to MySQL: uid=%s", user_id)
        return True

    # ============================================================ Redis 原语

    def _write_pending(self, user_id: int, payload: Dict[str, str]) -> None:
        """覆盖写 pending 暂存并把到期时间重置为 now+N 天（重新计时）。

        被谁调用：update_profile（手动）、_do_extract（自动）。
        参数：user_id (int)；payload——{profile_text, interests, topics}。
        返回：None。Redis 路径用 pipeline 原子执行 HSET + EXPIRE + ZADD；
        pending 保底 TTL = settings.PROFILE_PENDING_TTL_DAYS（默认 7）天
        再加 1 天（防 due zset 成员丢失产生永久脏暂存）。
        异常：Redis 异常走进程内降级；降级也失败仅记 error 日志。
        """
        due = time.time() + settings.PROFILE_PENDING_TTL_DAYS * 86400
        client = self.client
        try:
            if client is not None:
                # pipeline 原子提交：暂存写入/保底 TTL/到期索引三者互为整体
                pipe = client.pipeline()
                pipe.hset(_pending_key(user_id), mapping=payload)
                # 保底 TTL（防 due 丢失产生永久脏暂存），略宽于 7 天
                pipe.expire(_pending_key(user_id), settings.PROFILE_PENDING_TTL_DAYS * 86400 + 86400)
                pipe.zadd(_DUE_KEY, {str(user_id): due})
                pipe.execute()
                return
            with self._lock:
                # 降级模式：dict 覆盖暂存 + 记录到期时间戳
                self._mem_pending[user_id] = dict(payload)
                self._mem_due[user_id] = due
        except Exception as e:
            logger.error("profile write pending failed: %s", e)

    def _read_pending(self, user_id: int) -> Optional[Dict[str, str]]:
        """读取 pending 暂存三字段。

        被谁调用：get_profile（覆盖 MySQL 基线）、_flush_one（落库内容来源）。
        参数：user_id (int)。返回：Dict[str,str]|None——暂存不存在返回 None
        （调用方据此回退 MySQL 基线）；Redis 异常也返回 None（按无暂存处理）。
        """
        client = self.client
        try:
            if client is not None:
                data = client.hgetall(_pending_key(user_id))
                if not data:
                    return None
                return {k: data.get(k, "") for k in _FIELDS}
            with self._lock:
                data = self._mem_pending.get(user_id)
                return dict(data) if data else None
        except Exception as e:
            logger.error("profile read pending failed: %s", e)
            return None

    def _delete_pending(self, user_id: int) -> None:
        """删除 pending hash（落库成功后的清理步骤之一）。异常仅记 debug 日志。"""
        client = self.client
        try:
            if client is not None:
                client.delete(_pending_key(user_id))
                return
            with self._lock:
                self._mem_pending.pop(user_id, None)
        except Exception as e:
            logger.debug("profile delete pending failed: %s", e)

    def _due_at(self, user_id: int) -> Optional[float]:
        """读取该用户 pending 的应落库时间戳（due zset score）。

        被谁调用：get_profile（回传前端 due_at）、_flush_if_due（惰性判定）。
        返回：float|None——Unix 时间戳；无索引/异常时返回 None。
        """
        client = self.client
        try:
            if client is not None:
                score = client.zscore(_DUE_KEY, str(user_id))
                return float(score) if score is not None else None
            with self._lock:
                return self._mem_due.get(user_id)
        except Exception as e:
            logger.debug("profile due_at failed: %s", e)
            return None

    def _remove_due(self, user_id: int) -> None:
        """从到期索引 zset 移除该用户（落库成功或无 pending 清脏索引时）。

        异常仅记 debug 日志（残留成员下轮 flush 时会因 pending 缺失再被清理）。
        """
        client = self.client
        try:
            if client is not None:
                client.zrem(_DUE_KEY, str(user_id))
                return
            with self._lock:
                self._mem_due.pop(user_id, None)
        except Exception as e:
            logger.debug("profile remove due failed: %s", e)

    def _acquire_throttle(self, user_id: int) -> bool:
        """SET NX EX：窗口期内已有提取任务则跳过。

        被谁调用：extract_from_conversation_async 提交线程池任务前。
        参数：user_id (int)。返回：bool——True 抢到提取名额（首次/窗口外），
        False 节流窗口 settings.PROFILE_EXTRACT_INTERVAL_SECONDS（默认 600 秒）
        内已有任务。降级模式按 _mem_throttle 时间戳判定。
        异常：节流组件故障时返回 True——宁可重复提取也不丢失画像更新。
        """
        client = self.client
        ttl = settings.PROFILE_EXTRACT_INTERVAL_SECONDS
        key = _throttle_key(user_id)
        try:
            if client is not None:
                return bool(client.set(key, "1", nx=True, ex=ttl))
            with self._lock:
                now = time.time()
                if now < self._mem_throttle.get(user_id, 0):
                    return False
                self._mem_throttle[user_id] = now + ttl
                return True
        except Exception as e:
            # 节流故障不阻断画像提取（宁可重复也不丢失画像更新）
            logger.debug("profile throttle failed: %s", e)
            return True

    @staticmethod
    def _empty(user_id: int) -> Dict[str, Any]:
        """构造空画像响应（user_id 非法等场景）；结构与 get_profile 正常返回一致。"""
        return {
            "user_id": user_id,
            "profile_text": "",
            "interests": "",
            "topics": "",
            "pending": False,
            "due_at": None,
        }


@lru_cache(maxsize=1)
def get_profile_service() -> ProfileService:
    """应用级单例工厂（@lru_cache(maxsize=1)，首次调用无参构造并缓存）。

    被谁调用：app/application/chat/chat_service.py（ChatService.__init__）、
    app/api/v1/profile.py（GET/PUT /profile）、control/app.py
    lifespan（启动后台落库线程）。
    返回：ProfileService——进程内共享单例。
    """
    return ProfileService()


def reset_profile_service_for_test() -> None:
    """测试辅助：清空 lru_cache 单例缓存（配合 fake Redis/DAO 重新构造）。"""
    get_profile_service.cache_clear()
