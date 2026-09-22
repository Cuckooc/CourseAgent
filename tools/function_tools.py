"""
模块名：tools.function_tools

作用：
    上一代 ReAct 形态的函数工具集（LangChain @tool 装饰器），供
    VagueAgent / AnalysisAgent 的 ReAct 循环调用。每个工具只做"判定/兜底"
    并返回一句中文结论文本，由 ReAct Agent 读取后决定下一步走向。

v2 改造：将纯关键词硬匹配升级为「关键词 + 语义感知启发式」混合判定，
围绕「课程咨询助手」场景设计，降低误分类导致的答非所问。

- vague：不再仅因出现疑问词就判模糊，结合问题长度、疑问词密度、是否有具体主题综合判定；
- course：不再依赖 100+ 关键词硬匹配，改为「核心关键词 OR 学科主题名词 OR 疑问结构 + 教育上位词」
  多路启发式，只要问题语义上涉及学习/课程/知识就倾向调用 RAG（宁可多检索，由检索层 rerank 把关）；
- file：检查实际上传目录（settings.UPLOAD_DIR + 会话临时目录），而非错误的 file_analysis/。

工具分类与统一封装：
- 每个工具标注 category（analysis / retrieval / action）
- ToolResult 数据类统一封装执行结果（success / data / error / elapsed_ms）

主要成员（7 个 @tool + 1 个执行包装 + 3 个启发式辅助函数）：
    - vague：判断用户问题是否模糊、是否需要澄清（analysis）；
    - predict：对模糊问题发起多轮澄清追问（analysis）；
    - course：判断是否需要 RAGAgent 知识库检索（retrieval）；
    - file：判断是否存在上传文件、是否需要 FileAgent（retrieval）；
    - summary：对信息做总结提炼（action）；
    - chat：兜底闲聊回复（action）；
    - common：礼貌用语等可直接回答场景的判断（analysis）；
    - execute_tool：统一计时 + 异常捕获 + 标准化 ToolResult 的执行包装；
    - _has_topic_noun / _has_question_word / _has_temp_session_files：启发式辅助。

被谁使用（现状）：
    - 早期由 VagueAgent / AnalysisAgent 经 ReAct（create_react_agent）调用；
      当前主链路（multi_agent/、service/）已不再 import 本模块，判定改由
      LLM 直出 + Agent 规则完成（vague_agent.py 内仅模拟 vague 工具的输出结构）；
    - 现仅作为"规则基线"被评估/回归脚本调用：
      tests/phase/test_all_changes.py（vague/course/file 语义判定）、
      tests/phase/test_phase8_agent_eval.py（vague/course/summary/execute_tool）；
    - 模块保留为未来恢复 function calling 的基础设施，计划 P3 阶段评估删除。
"""
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain.tools import tool

from core.config import settings

# ==================== 工具分类 ====================

# 工具名 → 工具类别映射（模块级常量）：
#   analysis  意图分析/判定类，输出倾向"下一步该做什么"的结论；
#   retrieval 检索路由类，输出是否需要 RAGAgent / FileAgent；
#   action    动作类（总结/闲聊）。
# 被 execute_tool() 读取以填充 ToolResult.category；键名必须与
# 各 @tool 装饰函数名（LangChain 工具名）一致，未命中归为 "unknown"。
TOOL_CATEGORIES = {
    "vague": "analysis",      # 意图分析：问题是否模糊、是否需要澄清
    "predict": "analysis",    # 意图澄清：对模糊问题发起追问
    "course": "retrieval",    # 知识库检索判断：是否路由 RAGAgent
    "file": "retrieval",      # 文件检索判断：是否路由 FileAgent
    "summary": "action",      # 总结提炼
    "chat": "action",         # 对话回复
    "common": "analysis",     # 通用判断：礼貌用语等可否直接回答
}


@dataclass
class ToolResult:
    """旧式 ReAct 工具执行结果的统一封装：标准化返回格式 + 耗时统计。

    类作用：与 tools/protocol.py 的 ToolResult 同名但互不相干——本类服务于
    上一代 @tool 函数工具集，由 execute_tool() 统一产出，把"成功数据/失败
    原因/耗时/工具名/类别"收敛为固定结构，便于 ReAct Agent 或评估脚本一致消费。
    被谁使用：execute_tool() 构造；tests/phase/test_phase8_agent_eval.py
              断言其 success/error 等字段；经 to_dict() 可直接序列化进日志/报告。
    """

    # 是否执行成功（工具内部抛异常时为 False）
    success: bool = True
    # 工具返回值：本模块各工具均为中文结论文本字符串
    data: Any = None
    # 失败原因文本（异常 str），成功时为 None
    error: Optional[str] = None
    # 执行耗时（毫秒），由 execute_tool 用 time.perf_counter 实测
    elapsed_ms: int = 0
    # 实际执行的工具名（LangChain BaseTool.name 或函数 __name__）
    tool_name: str = ""
    # 工具类别，取自 TOOL_CATEGORIES（analysis/retrieval/action/unknown）
    category: str = ""

    def to_dict(self):
        """把结果序列化为普通 dict（无嵌套对象，可直接 JSON 化）。

        被谁调用：需要把工具执行结果写入日志/评估报告或跨进程传递时由调用方使用。
        返回：与字段一一对应的 dict（success/data/error/elapsed_ms/
              tool_name/category），去向为日志、评估输出或上游消费方。
        """
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "tool_name": self.tool_name,
            "category": self.category,
        }


def execute_tool(tool_fn, **kwargs):
    # type: (...) -> ToolResult
    """统一工具执行包装：计时 + 异常捕获 + 标准化返回。

    被谁调用：tests/phase/test_phase8_agent_eval.py 批量评估 vague/course/
              summary 等工具时调用；早期 ReAct 编排可借此统一收口，现保留为
              基础设施。
    参数：
        tool_fn：@tool 装饰后的 LangChain BaseTool（优先取其 .name）或普通函数；
        **kwargs：透传给工具的入参（如 query="..."），对应工具的入参 schema；
                  不传任何参数时以空 dict 调用。
    返回：ToolResult——成功时 data 为工具输出的中文结论文本；任何异常都不外抛，
          收口为 success=False、error=异常信息，保证评估/编排循环不中断。
    """
    name = getattr(tool_fn, "name", tool_fn.__name__ if hasattr(tool_fn, "__name__") else "unknown")
    category = TOOL_CATEGORIES.get(name, "unknown")
    start = time.perf_counter()
    try:
        result = tool_fn.invoke(kwargs) if kwargs else tool_fn.invoke({})
        elapsed = int((time.perf_counter() - start) * 1000)
        return ToolResult(
            success=True,
            data=result,
            elapsed_ms=elapsed,
            tool_name=name,
            category=category,
        )
    except Exception as e:
        elapsed = int((time.perf_counter() - start) * 1000)
        return ToolResult(
            success=False,
            error=str(e),
            elapsed_ms=elapsed,
            tool_name=name,
            category=category,
        )

# ==================== vague 判定常量 ====================

# 强模糊词：单独出现就倾向需要澄清（不含具体主题时）
_VAGUE_STRONG = [
    "怎么样", "如何", "哪些", "哪里", "哪个", "是否", "能否",
    "可以吗", "需要吗", "应该吗", "有没有", "是否存在", "是否可以", "是否需要",
]

# 弱模糊词：需与其他信号组合才构成模糊
_VAGUE_WEAK = ["什么", "谁", "多少", "几时", "为什么"]

# 具体主题信号词：出现时说明问题有明确主题，不应判为模糊
# 注意：不含"课程""专业"等上位词——"什么课程好"仍属模糊，需澄清具体方向
_TOPIC_SIGNALS = [
    "学习", "考试", "成绩", "复习", "备考", "练习", "训练",
    "高中", "初中", "大学", "考研", "雅思", "托福", "四六级",
    "数学", "物理", "化学", "英语", "语文", "生物", "历史", "政治", "地理",
    "编程", "Python", "Java", "算法", "数据库", "前端", "后端",
]


# ==================== course 判定常量 ====================

# 核心课程关键词（高置信度：命中即判 RAG）
_COURSE_KEYWORDS = [
    # 学习规划/方法
    "学习规划", "学习计划", "学习路径", "学习方法", "学习建议",
    "学习效果", "学习进度", "时间安排",
    # 学科
    "数学", "物理", "化学", "地理", "英语", "生物", "历史", "政治", "语文",
    "编程", "后端", "前端", "算法", "数据库", "人工智能", "机器学习", "深度学习",
    "数据分析", "爬虫", "运维", "云计算", "大数据",
    # 备考
    "高考", "考研", "雅思", "托福", "四六级", "竞赛",
    # 学习行为
    "基础巩固", "能力提升", "冲刺备考", "知识体系", "错题分析",
    "刷题", "真题", "模拟考试", "思维导图", "查漏补缺",
    # 教育上位词
    "课程", "教材", "专业", "学科", "培训", "辅导",
]

# 学科/领域主题名词（与疑问结构配合时判定为课程问题）
_TOPIC_NOUNS = {
    # 学科
    "数学", "物理", "化学", "地理", "英语", "语文", "生物", "历史", "政治",
    "编程", "代码", "算法", "数据库", "前端", "后端", "运维",
    "人工智能", "机器学习", "深度学习", "数据分析", "爬虫",
    # 编程语言
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C++", "C语言",
    "SQL", "Ruby", "Swift", "Kotlin",
    # 乐器/体育/艺术
    "钢琴", "吉他", "小提琴", "篮球", "足球", "乒乓球", "羽毛球", "游泳", "芭蕾",
    # 考试
    "高考", "考研", "雅思", "托福", "四六级", "竞赛", "国考",
    # 教育概念（不含"课程""专业"等上位词——太泛，"什么课程好"仍应判模糊）
    "学历", "学位", "毕业", "留学",
}

# 疑问句结构词（表示用户在提问）
_QUESTION_WORDS = [
    "怎么", "怎样", "如何", "什么", "哪些", "哪里", "哪个",
    "为什么", "是否", "能不能", "可以吗", "需要", "应该",
    "有没有", "多少", "几", "吗", "呢", "么",
]


# ==================== 辅助函数 ====================

def _has_topic_noun(query: str) -> bool:
    """检测查询是否包含具体主题名词（学科/领域/考试等）。

    被谁调用：vague()（有具体主题即判明确）与 course()（主题名词是 RAG
              判定信号之一）。
    参数：query 为已 strip 的用户问题文本。
    返回：两路命中任一即 True——①子串直配 _TOPIC_NOUNS；②jieba 分词后
          逐词精确匹配（避免"Java"误配子串类问题，分词失败则静默跳过该路）。
    """
    for noun in _TOPIC_NOUNS:
        if noun in query:
            return True
    try:
        # 分词精确匹配：jieba 不可用/分词异常时降级为仅子串匹配结果
        import jieba
        for seg in jieba.cut(query):
            if seg in _TOPIC_NOUNS:
                return True
    except Exception:
        pass
    return False


def _has_question_word(query: str) -> bool:
    """检测查询是否包含疑问句结构词。

    被谁调用：course()，与主题名词/教育上位词组合构成 RAG 判定启发式。
    参数：query 为已 strip 的用户问题文本。
    返回：命中 _QUESTION_WORDS 任一疑问结构词即 True，否则 False。
    """
    for w in _QUESTION_WORDS:
        if w in query:
            return True
    return False


def _has_temp_session_files(user_id: int, session_id: int) -> bool:
    """检查指定会话的临时上传目录中是否存在已上传文件。

    被谁调用：当前无调用方（file 工具内联了等价扫描逻辑），作为按
              user_id/session_id 精确判断的辅助能力保留，供后续复用。
    参数：user_id 用户 ID、session_id 会话 ID，来源服务端会话状态；
          临时目录规则为 settings.UPLOAD_DIR/temp/{user_id}_{session_id}。
    返回：目录存在且其中至少有一个普通文件时 True；缺身份/无目录/空目录 False。
    """
    if not user_id or not session_id:
        return False
    temp_dir = settings.UPLOAD_DIR / "temp" / f"{user_id}_{session_id}"
    if not temp_dir.is_dir():
        return False
    return any(f.is_file() for f in temp_dir.iterdir())


# ==================== 工具函数 ====================

@tool
def vague(query: str) -> str:
    """判断用户输入的问题是否模糊需要澄清。
    围绕课程咨询场景优化：有具体主题的问题即使含疑问词也不判为模糊。
    参数: query (str, 用户问题)

    工具名：vague（category=analysis）。
    入参 schema：{"query": str}——用户原始问题，来源 ReAct 循环传入的用户 query。
    数据源：无外部数据源；仅用模块内启发式常量（_VAGUE_STRONG/_VAGUE_WEAK/
            _TOPIC_SIGNALS）与 _has_topic_noun 做规则判定。
    输出结构：固定中文结论文本（"…模糊…需要进一步澄清" 或 "…意图较为明确…"），
              消费方靠包含"模糊/明确"关键字解析布尔结论：早期为 VagueAgent 的
              ReAct 循环，现为 tests/phase/test_all_changes.py、
              test_phase8_agent_eval.py 的金标准评估（multi_agent/vague_agent.py
              仅模拟同款输出结构，不再 import 本工具）。
    """
    query = query.strip()
    if not query:
        return "用户未输入有效内容，可能需要引导"

    # 极短且无具体信号 → 模糊
    if len(query) <= 3:
        return "用户输入过于简短且无具体主题，可能存在模糊意图，需要进一步澄清"

    # 统计模糊词命中
    strong_count = sum(1 for p in _VAGUE_STRONG if p in query)
    weak_count = sum(1 for p in _VAGUE_WEAK if p in query)

    # 高密度模糊词 + 无具体主题信号 → 模糊
    if strong_count >= 2 and not any(s in query for s in _TOPIC_SIGNALS):
        return "用户输入包含多个模糊表达且无具体主题，可能存在模糊意图，需要进一步澄清"

    # 有具体主题名词 → 明确（即使含疑问词，也是在问具体的事）
    if _has_topic_noun(query):
        return "用户输入包含具体主题，意图较为明确，不需要进一步澄清"

    # 有具体主题信号词 → 明确
    if any(s in query for s in _TOPIC_SIGNALS):
        return "用户输入包含具体主题信号，意图较为明确，不需要进一步澄清"

    # 短问题 + 弱模糊词 → 模糊
    if len(query) <= 8 and weak_count > 0:
        return "用户输入过于简短且包含疑问词，可能存在模糊意图，需要进一步澄清"

    # 单一强模糊词 + 无主题 → 偏模糊
    if strong_count >= 1 and weak_count >= 1 and len(query) <= 15:
        return "用户输入包含模糊表达且较短无主题，可能存在模糊意图，需要进一步澄清"

    return "用户输入的问题意图较为明确，不需要进一步澄清"


@tool
def course(query: str) -> str:
    """判断用户问题是否涉及课程/学习/知识，需要调用知识库检索。
    围绕课程咨询场景设计：关键词命中 OR 学科主题名词 OR 疑问结构+教育词 均可触发。
    参数: query (str, 用户问题)

    工具名：course（category=retrieval）。
    入参 schema：{"query": str}——用户问题，来源用户 query（经 ReAct 决策传入）。
    数据源：无外部数据源；多路启发式信号均取自模块常量
            （_COURSE_KEYWORDS 核心关键词、_TOPIC_NOUNS 学科主题名词、
            _QUESTION_WORDS 疑问结构、函数内 edu_signals 教育上位词）。
    输出结构：固定中文结论文本，明确包含"需要调用RAGAgent进行知识库检索"或
              "不需要调用RAGAgent"，消费方据此做检索路由；早期消费方为
              AnalysisAgent 的 ReAct 循环，现为 tests/phase/test_all_changes.py、
              test_phase8_agent_eval.py 的规则基线评估。本工具只判定"要不要检索"，
              不直接访问向量库/知识库。
    """
    query = query.strip()
    if not query:
        return "用户未输入有效内容，不满足使用知识库条件"

    # 精确关键词命中（高置信度）
    hit_kw = None
    for kw in _COURSE_KEYWORDS:
        if kw in query:
            hit_kw = kw
            break

    # 学科/领域主题名词命中
    has_topic = _has_topic_noun(query)

    # 疑问句结构
    has_question = _has_question_word(query)

    # 教育上位词
    edu_signals = ["学习", "课程", "教育", "培训", "辅导", "提升", "进步", "知识", "入门"]
    has_edu = any(e in query for e in edu_signals)

    # 判定逻辑
    if hit_kw:
        return (
            f"用户问题涉及课程关键词「{hit_kw}」，"
            "满足使用知识库条件，需要调用RAGAgent进行知识库检索"
        )

    if has_topic and has_question:
        return (
            "用户问题涉及具体学科/领域且为提问形式，"
            "满足使用知识库条件，需要调用RAGAgent进行知识库检索"
        )

    if has_topic and has_edu:
        return (
            "用户问题涉及具体学科/领域且包含学习相关表达，"
            "满足使用知识库条件，需要调用RAGAgent进行知识库检索"
        )

    if has_question and has_edu and len(query) > 4:
        return (
            "用户问题为教育类提问，"
            "满足使用知识库条件，需要调用RAGAgent进行知识库检索"
        )

    if not has_question and not has_edu:
        return "用户问题与课程学习无关，不满足使用知识库条件，不需要调用RAGAgent"

    return "用户问题不明确涉及课程学习，不满足使用知识库条件，不需要调用RAGAgent"


@tool
def file(query: str, file_path: str = "") -> str:
    """判断用户是否上传文件以及是否使用文件知识库。
    检查实际上传目录（UPLOAD_DIR + 会话临时目录），而非硬编码路径。
    参数: query (str, 用户问题), file_path (str, 文件路径)

    工具名：file（category=retrieval）。
    入参 schema：{"query": str, "file_path": str, 可选}——query 为用户问题
                （本工具不基于 query 内容判定，仅保留统一签名），file_path
                为 LLM/上游显式给出的文件路径，缺省空串。
    数据源：文件系统，按以下顺序探测——①file_path 指向的文件；
            ②settings.UPLOAD_DIR 根目录下最近创建的合规文件
            （service 上传落盘目录，与 dao/knowledge、vector_store 使用的
            上传根目录一致）；③UPLOAD_DIR/temp/<会话目录> 下的会话临时文件；
            ④历史兼容目录 file_analysis/。仅识别 _ALLOWED_EXTS 白名单后缀。
    输出结构：固定中文结论文本，包含"需要调用FileAgent进行文件处理"或
              "不需要调用FileAgent"，消费方据此做文件检索路由；早期消费方为
              AnalysisAgent 的 ReAct 循环，现为 tests/phase/test_all_changes.py
              的回归基线。本工具只探测文件存在性，不解析文件内容。
    """
    # 允许识别的上传文件后缀白名单（PDF/Office/文本/网页/Notebook/Markdown）
    _ALLOWED_EXTS = (".pdf", ".docx", ".txt", ".pptx", ".html", ".ipynb", ".md")

    if file_path and os.path.exists(file_path):
        return f"用户上传了文件，文件路径为{file_path}，满足使用文件工具条件信息，需要调用FileAgent进行文件处理"

    # 探测②：上传根目录，取创建时间（st_ctime）最新的合规文件
    upload_dir = settings.UPLOAD_DIR
    if upload_dir.is_dir():
        files = [f for f in upload_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in _ALLOWED_EXTS]
        if files:
            # 多个上传文件时以最近创建者为准（max + ctime）
            latest = max(files, key=lambda f: f.stat().st_ctime)
            return (
                f"检测到上传的文件{latest.name}，满足使用文件工具条件信息，"
                "需要调用FileAgent进行文件处理"
            )

        # 检查会话临时目录（任意会话有文件即视为有上传）
        temp_root = upload_dir / "temp"
        if temp_root.is_dir():
            for session_dir in temp_root.iterdir():
                if session_dir.is_dir():
                    session_files = [
                        f for f in session_dir.iterdir()
                        if f.is_file() and f.suffix.lower() in _ALLOWED_EXTS
                    ]
                    if session_files:
                        return (
                            f"检测到会话临时文件{session_files[0].name}，"
                            "满足使用文件工具条件信息，需要调用FileAgent进行文件处理"
                        )

    # 兼容旧路径 file_analysis/
    legacy_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "file_analysis",
    )
    if os.path.isdir(legacy_dir):
        legacy_files = [
            f for f in os.listdir(legacy_dir)
            if f.lower().endswith(_ALLOWED_EXTS)
        ]
        if legacy_files:
            return (
                f"检测到默认文件{legacy_files[0]}，满足使用文件工具条件信息，"
                "需要调用FileAgent进行文件处理"
            )

    return "未检测到上传文件，不满足使用文件工具条件信息，不需要调用FileAgent进行文件处理"


@tool
def predict(query: str) -> str:
    """根据用户输入的问题进行多轮提问
    参数: query (str, 用户问题)

    工具名：predict（category=analysis）。
    入参 schema：{"query": str}——被判模糊的用户问题，来源 vague 判定后的澄清分支。
    数据源：无；纯模板化追问，不调用 LLM/知识库。
    输出结构：固定中文追问模板字符串（回显 query 并请用户补充需求），
              消费方为早期 VagueAgent/PredictAgent 的 ReAct 循环（澄清话术）。
    """
    return f"正在对用户问题「{query}」进行意图澄清，请补充更详细的需求。"


@tool
def summary(query: str) -> str:
    """根据用户输入的信息进行总结提炼
    参数: query (str, 用户问题)

    工具名：summary（category=action）。
    入参 schema：{"query": str}——待总结的信息文本，来源上游聚合后的内容；
                空串/空值走"无法总结"兜底分支。
    数据源：无外部数据源；仅按固定模板包装输入文本（真正的摘要由 SummaryAgent
            与 LLM 完成，本工具只提供 ReAct 形态的占位动作）。
    输出结构：多行中文模板字符串（【核心总结】+【关键信息】），消费方为早期
              ReAct 循环与 tests/phase/test_phase8_agent_eval.py 评估。
    """
    if not query:
        return "未获取到有效信息，无法进行总结"
    return (
        f"信息总结完成：\n"
        f"【核心总结】：对输入内容进行归纳整理，提炼核心内容与关键信息\n"
        f"【关键信息】：{query}"
    )


@tool
def chat() -> str:
    """根据用户输入的信息进行聊天回复
    参数: 无

    工具名：chat（category=action）。
    入参 schema：无参数（LangChain 工具签名中为空）。
    数据源：无；纯兜底占位回复，不调用 LLM（真正闲聊由 ChatAgent 完成）。
    输出结构：固定中文提示字符串，消费方为早期 ChatAgent 的 ReAct 循环，
              表示"已路由到闲聊、请等待回复"。
    """
    return "正在根据用户输入的信息进行聊天回复，请稍候。"


@tool
def common(query: str) -> str:
    """判断用户输入的信息是否可以直接回答
    参数: query (str, 用户问题)

    工具名：common（category=analysis）。
    入参 schema：{"query": str}——用户问题，来源用户 query。
    数据源：无外部数据源；仅与函数内 key_word 礼貌用语词表
            （问候/感谢/求助类）做子串匹配。
    输出结构：固定中文结论文本（"可以直接回答，不需要调用工具" 或
              "需要调用工具"），消费方为早期 AnalysisAgent 的 ReAct 循环，
              用于拦截问候/感谢等无需检索的轻量交互。
    """
    key_word = [
        "你好", "您好", "请问", "麻烦", "帮我", "能否帮我", "可以帮我吗",
        "请帮我", "请你帮我", "请您帮我", "谢谢", "感谢", "多谢", "非常感谢",
        "请教", "求教", "求助", "帮忙"
    ]
    for word in key_word:
        if word in query:
            return f"用户输入的问题包含关键词'{word}'，可以直接回答，不需要调用工具"
    return "用户输入的问题不包含关键词，不能直接回答，需要调用工具"
