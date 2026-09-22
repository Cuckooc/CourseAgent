"""
模块名：service.preference_service
作用：用户偏好提取服务。临时知识库文件上传后，从脱敏文本中用 LLM 异步
      提取用户偏好、习惯等关键信息（学习/工作领域、兴趣话题、表达习惯、
      关注方向），结果存入进程内 per-user_id 内存缓存，供后续对话个性化使用。
      仅分析脱敏后的文本，不接触原始敏感信息。

主要成员：
- extract_preferences(text, user_id)：异步提交偏好提取任务（立即返回）。
- _do_extract(text, user_id)：线程池任务体：调 LLM 提取并写缓存。
- get_preferences(user_id)：读取某用户的缓存偏好。
- get_preference_service()：保留的单例入口（当前返回 None，模块级函数即可）。
- _preference_cache/_cache_lock/_pool/_PREFERENCE_PROMPT：模块级缓存、锁、
  线程池与提取提示词常量。

被谁使用：
- control/file_control.py：_process_saved_file 中 temp 文件处理成功后
  调 extract_preferences(masked_text, user_id)（fire-and-forget）。
- get_preferences 当前仓库内无业务调用点，作为个性化能力的读取入口预留。
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Dict, Optional

from model_llm.gateway import build_chat_model

# 模块级日志器：偏好提取成功/失败日志走该 logger
logger = logging.getLogger(__name__)

# 模块级全局对象：偏好缓存 user_id -> preference dict（进程内，线程安全由 _cache_lock 保护）
_preference_cache: Dict[int, dict] = {}
# 模块级全局对象：保护 _preference_cache 读写的互斥锁
_cache_lock = threading.Lock()

# 模块级全局对象：偏好提取专用线程池（2 worker，不阻塞上传响应）
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pref")

# 模块级常量：偏好提取的 system/user 提示词模板，{text} 占位待填脱敏文本
_PREFERENCE_PROMPT = """你是一个用户偏好分析助手。请从以下文本中提取该用户的偏好与习惯信息，包括但不限于：
- 学习/工作领域
- 感兴趣的话题
- 表达习惯（正式/口语/简洁/详细）
- 关注的重点方向

请用简短的要点列表输出，每条不超过20字。如果文本中没有明显的偏好信息，回复"无明显偏好"。

文本内容：
{text}

偏好要点："""


def extract_preferences(text: str, user_id: int) -> None:
    """异步提取用户偏好并写入缓存（不阻塞调用方）。

    功能：空文本直接忽略；否则把任务提交到 _pool 线程池后台执行，
          调用方（上传链路）立即返回，不感知提取成败。文本截断前 8000
          字以控制单次 LLM token 消耗。
    被谁调用：control/file_control.py 的 _process_saved_file（temp 文件
              上传成功且文本已脱敏后，fire-and-forget）。
    参数：
    - text (str)：脱敏后的文件提取文本（来源：file_control 中 mask_text
      的输出，禁止传原文）。
    - user_id (int)：上传用户 ID（JWT 注入），缓存归属键。
    返回：无。结果去向：_do_extract 写入模块级 _preference_cache。
    """
    if not text or not text.strip():
        return
    # 截断保护：仅取前 8000 字送 LLM，偏好信号集中在文本前部且可控制成本
    _pool.submit(_do_extract, text[:8000], user_id)


def _do_extract(text: str, user_id: int) -> None:
    """偏好提取线程池任务体：调 LLM 提取偏好要点并写入缓存。

    功能：构建聊天模型、用 _PREFERENCE_PROMPT 发起一次 LLM 调用，取响应
          文本（兼容 .content 属性或字符串响应），加锁写入
          _preference_cache[user_id]。
    被谁调用：由 extract_preferences() 提交到 _pool 异步执行。
    参数：text (str)——截断后的脱敏文本；user_id (int)——缓存归属用户。
    返回：无。数据来源：model_llm/gateway.build_chat_model 的 LLM 返回；
          去向：进程内 _preference_cache（source 标记 temp_knowledge）。
    异常：LLM 调用失败仅记 error 日志，不重试、不影响上传主流程。
    """
    try:
        llm = build_chat_model()
        resp = llm.invoke(_PREFERENCE_PROMPT.format(text=text))
        content = getattr(resp, "content", "") or str(resp)
        with _cache_lock:
            _preference_cache[user_id] = {"preferences": content.strip(), "source": "temp_knowledge"}
        logger.info("preference extracted for user %s: %s", user_id, content[:100])
    except Exception as e:
        logger.error("preference extraction failed for user %s: %s", user_id, e)


def get_preferences(user_id: int) -> Optional[dict]:
    """读取指定用户的缓存偏好（加锁，线程安全）。

    功能：从 _preference_cache 取出该用户最近一次提取结果。
    被谁调用：当前仓库内无业务调用点，作为后续对话个性化的读取入口预留。
    参数：user_id (int)——用户 ID。
    返回：Optional[dict]——{"preferences": 要点文本, "source":
          "temp_knowledge"}；从未提取或进程重启后为 None（缓存不持久化）。
    """
    with _cache_lock:
        return _preference_cache.get(user_id)


@lru_cache(maxsize=1)
def get_preference_service():
    """保留的单例风格入口（兼容统一 service 获取习惯）。

    功能：本模块能力全部由模块级函数承载，无需实例状态，故恒返回 None；
          lru_cache 仅保留入口形态，调用方应直接使用 extract_preferences /
          get_preferences。
    被谁调用：当前仓库内无调用点（预留）。
    返回：None。
    """
    return None  # 模块级函数即可，保留单例入口供统一调用
