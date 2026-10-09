"""
模块名：core.content_filter

作用：
    输出内容过滤。对 AI 最终输出做关键词黑名单替换（命中词替换为 ***）。
    词库来自 core/resources/banned_words.txt（每行一个，# 开头为注释），首次使用时惰性加载并缓存。
    过滤失败时放行（不阻断主流程），仅记录告警日志。

主要成员：
    - filter_text(text)：对外唯一入口，替换文本中的敏感词；
    - _load_words()：内部函数，惰性加载/缓存黑名单词表；
    - _BANNED_WORDS_PATH：模块级常量，词库文件路径（<根>/core/resources/banned_words.txt）；
    - _words_cache：模块级全局单例，加载后的词表缓存（None=尚未加载，[]=词库为空或加载失败）。

被谁使用：
    - app/application/chat/chat_service.py：非流式结果（ai_output 返回前）与流式分片写出前两处调用。
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 模块级常量：敏感词库文件绝对路径（<项目根>/core/resources/banned_words.txt），导入期确定。
_BANNED_WORDS_PATH = Path(__file__).resolve().parent / "resources" / "banned_words.txt"

# 模块级全局单例：词表缓存。None 表示尚未加载（首次过滤时触发读文件），
# [] 表示词库文件不存在或读取异常（过滤器等价于关闭，直接放行）。
_words_cache = None  # type: list


def _load_words():
    # type: () -> List[str]
    """加载并缓存敏感词表（惰性、进程内缓存一次）。

    功能：首次调用时读取 _BANNED_WORDS_PATH，去掉空行与 # 注释行后缓存到 _words_cache；
    文件不存在或读取异常时缓存空列表（过滤降级为直通）。后续调用直接返回缓存。
    被谁调用：本模块 filter_text() 每次过滤前调用。
    参数：无。
    返回：List[str]，敏感词列表；任何异常下都返回列表（可能为空），不向调用方抛出。
    """
    global _words_cache
    if _words_cache is not None:
        return _words_cache
    try:
        if not _BANNED_WORDS_PATH.exists():
            _words_cache = []
            return _words_cache
        lines = _BANNED_WORDS_PATH.read_text(encoding="utf-8").splitlines()
        _words_cache = [
            line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        logger.info("Content filter loaded %d banned words", len(_words_cache))
    except Exception:
        logger.exception("Failed to load banned words, filter disabled")
        _words_cache = []
    return _words_cache


def filter_text(text):
    # type: (str) -> str
    """替换敏感词为 ***，过滤异常时原样返回。

    功能：逐个检查词表中的敏感词，命中即在文本中替换为 "***"（子串匹配，非词边界）。
    被谁调用：app/application/chat/chat_service.py —— 非流式最终输出 ai_output 落库/返回前，
              以及流式响应每个分片发送前。
    参数：
        text: 待过滤文本，来源为上游 service（LLM 生成内容，可能为空）。
    返回：
        str：过滤后的文本；text 为空、词表为空或过滤异常时原样返回（失败放行，不阻断对话）。
    """
    if not text:
        return text
    words = _load_words()
    if not words:
        return text
    try:
        result = text
        for w in words:
            if w in result:
                result = result.replace(w, "***")
        return result
    except Exception:
        logger.warning("Content filter failed, passing through original text")
        return text
