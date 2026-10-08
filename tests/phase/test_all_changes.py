"""
模块名：tests/phase/test_all_changes.py。

本次改动的纯单元集成脚本测试（脚本式用例：模块导入即顺序执行 9 个段落，
全局 PASS/FAIL + check() 汇总，末尾 FAIL>0 时 sys.exit(1)；
pytest 收集时同样按脚本执行）。不依赖外部 LLM / DashScope API / MySQL / Redis：
prompt 检查只做字符串包含；agent 行为用本地临时目录；源码契约用 inspect 静态比对。

覆盖段落（内嵌测试点是模块级语句，非 pytest test_ 函数）：
1. retrieval.py — 混合检索（关键词提取、RRF 融合、candidate_key 去重、scope 过滤）
2. function_app.domain.tools.py — vague / course / file 工具语义判定（内联金标用例表）
3. llm_business.py — 各 prompt 模板包含课程咨询领域语义指引
4. analysis_agent.py — _check_uploaded_files 检查 UPLOAD_DIR 与 temp 会话目录
5. file_agent.py — handle() 在 db=None 时仍可检索 temp store（不抛异常）
6. agent_service.py — FileAgent 不再注入 shared_db（inspect 源码契约）
7. 安全机制 — 非关键 Agent 不走 FailureDiagnoser（inspect 源码契约）
8. 安全机制 — SummaryAgent 相关性回退（0.6 阈值、最多 3 轮、降级空结果）
9. verifier.py — score_relevance 存在且 LLM 不可用时默认 1.0 放行

被测对象来源：app/domain/agents/（retrieval、analysis_agent、file_agent、
message_bus、verifier、agent_service）、app/domain/tools/function_app.domain.tools.py、
model_llm/llm_business.py、core/config.py 的 UPLOAD_DIR。

运行方式：
    python tests/phase/test_all_changes.py
    # 可在任意有项目依赖的环境运行；文件头 sys.path.insert 注入脚本目录、
    # os.environ.setdefault("jwt_secret", ...) 提供配置默认值，二者不可改动
清理：第 4 节在 UPLOAD_DIR/temp 创建的测试文件均在 finally 中删除/移除空目录。
"""
import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("jwt_secret", "test-secret-for-validation")

# Port↔Adapter 组合根装配：本脚本直接实例化 AnalysisAgent/FileAgent 等业务类，
# 其构造经 application Port 取基础设施实现，必须先 import deps 完成注册
import app.api.deps  # noqa: F401

PASS = 0  # 全局通过断言计数
FAIL = 0  # 全局失败断言计数（末尾非 0 则 sys.exit(1)）


def check(name, condition):
    """断言辅助：累加全局 PASS/FAIL 并打印，不抛异常（保证 9 节全部跑完）。

    调用方：本脚本全部内嵌测试点。参数：name 用例名，condition 实际布尔判定。
    """
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# ====================== 1. retrieval.py ======================
print("\n=== 1. retrieval.py — 混合检索 ===")

from app.domain.agents.retrieval import (
    _extract_keywords,
    _rrf_merge,
    _candidate_key,
    _doc_matches_scope,
)
from langchain_core.documents import Document

# 1.1 关键词提取
kws = _extract_keywords("高中数学怎么提高")
check("关键词提取: 高中数学", len(kws) > 0)
check("关键词提取: 不含纯数字", all(not w.isdigit() for w in kws))

kws2 = _extract_keywords("Python入门学习路径")
check("关键词提取: Python相关", len(kws2) > 0)

kws3 = _extract_keywords("")
check("关键词提取: 空查询返回空列表", kws3 == [])

# 1.2 RRF 融合
doc_a = Document(page_content="数学学习方法", metadata={"doc_level": "parent", "parent_id": "p1"})
doc_b = Document(page_content="英语学习路径", metadata={"doc_level": "parent", "parent_id": "p2"})
doc_c = Document(page_content="物理备考策略", metadata={"doc_level": "parent", "parent_id": "p3"})

list1 = [(0.5, doc_a), (0.8, doc_b)]
list2 = [(0.3, doc_a), (0.6, doc_c)]

merged = _rrf_merge(list1, list2)
check("RRF 融合: 返回非空", len(merged) > 0)
check("RRF 融合: doc_a 排第一（两路都命中，RRF 分数最高）", merged[0][1] is doc_a)
check("RRF 融合: 去重后共 3 个文档", len(merged) == 3)

# 1.3 candidate_key
key_parent = _candidate_key(doc_a)
check("candidate_key: parent 类型", key_parent[0] == "parent")
check("candidate_key: parent_id 正确", key_parent[1] == "p1")

doc_legacy = Document(page_content="旧数据chunk", metadata={"source": "test.json"})
key_legacy = _candidate_key(doc_legacy)
check("candidate_key: legacy 类型", key_legacy[0] == "legacy")

# 1.4 scope 后过滤
doc_public = Document(page_content="公共知识", metadata={"scope": "public"})
doc_private_ok = Document(page_content="私有知识", metadata={"scope": "private", "user_id": 42})
doc_private_other = Document(page_content="他人私有", metadata={"scope": "private", "user_id": 99})

where = {"$or": [{"$and": [{"scope": "public"}]}, {"$and": [{"scope": "private"}, {"user_id": 42}]}]}
check("scope 过滤: public 可见", _doc_matches_scope(doc_public, where) is True)
check("scope 过滤: 本人 private 可见", _doc_matches_scope(doc_private_ok, where) is True)
check("scope 过滤: 他人 private 不可见", _doc_matches_scope(doc_private_other, where) is False)


# ====================== 2. function_app.domain.tools.py ======================
print("\n=== 2. function_app.domain.tools.py — 工具语义判定 ===")

from app.domain.tools.function_tools import vague, course, file as file_tool

# 2.1 vague 工具
# 金标数据（正常/边界）：(query, 期望模糊布尔, 意图说明)，前 3 组模糊正例、后 5 组明确负例
vague_cases = [
    ("什么课程好", True, "泛泛提问无具体主题"),
    ("怎么办", True, "极短无主题"),
    ("有什么好的", True, "模糊无方向"),
    ("高中数学怎么提高", False, "有具体学科"),
    ("Python入门学什么", False, "有具体编程语言"),
    ("高考复习计划", False, "有具体考试"),
    ("英语听力怎么练", False, "有具体学科+行为"),
    ("考研数学一怎么准备", False, "有具体考试+学科"),
]
for query, expect_vague, desc in vague_cases:
    result = vague.invoke(query)
    is_vague = "模糊" in result and "明确" not in result
    check(f"vague: \"{query}\" → {'模糊' if expect_vague else '明确'} ({desc})", is_vague == expect_vague)

# 2.2 course 工具
# 金标数据（正常/负例）：(query, 期望走 RAG 布尔, 意图说明)，前 8 组课程咨询正例、
# 后 3 组问候/天气/感谢等无关负例
course_cases = [
    ("高中数学怎么提高", True, "学科+提问"),
    ("Python入门学什么", True, "编程语言+提问"),
    ("高考复习计划", True, "考试关键词"),
    ("学习规划", True, "核心关键词"),
    ("帮我制定学习计划", True, "核心关键词"),
    ("英语听力怎么练", True, "学科+提问"),
    ("Java怎么学", True, "编程语言"),
    ("什么课程好", True, "课程关键词命中"),
    ("你好", False, "问候语"),
    ("今天天气怎么样", False, "无关话题"),
    ("谢谢帮助", False, "感谢语"),
]
for query, expect_rag, desc in course_cases:
    result = course.invoke(query)
    needs_rag = "需要调用RAGAgent" in result and "不需要调用RAGAgent" not in result
    check(f"course: \"{query}\" → {'RAG' if expect_rag else '跳过'} ({desc})", needs_rag == expect_rag)

# 2.3 file 工具（检查目录逻辑）
result_no_file = file_tool.invoke("测试无文件")
check("file: 返回有效结果（可能命中 legacy file_analysis/ 目录）", isinstance(result_no_file, str) and len(result_no_file) > 0)


# ====================== 3. llm_business.py ======================
print("\n=== 3. llm_business.py — prompt 领域适配 ===")

from app.infrastructure.llm.llm_business import (
    PredictLLM, AnalysisLLM, ChatLLM, RagLLM, FileLLM
)

predict_prompt = PredictLLM().generate()
check("PredictLLM: 包含课程咨询平台标识", "课程咨询服务" in predict_prompt)
check("PredictLLM: 包含模糊/明确判断要点", "模糊" in predict_prompt and "明确" in predict_prompt)
check("PredictLLM: 包含学科/领域示例", "学科" in predict_prompt)

analysis_prompt = AnalysisLLM().generate()
check("AnalysisLLM: 包含课程咨询平台标识", "课程咨询服务" in analysis_prompt)
check("AnalysisLLM: 强调语义理解", "语义" in analysis_prompt)
check("AnalysisLLM: 包含学科学习范畴", "学科学习" in analysis_prompt)
check("AnalysisLLM: 包含备考指导范畴", "备考指导" in analysis_prompt)
check("AnalysisLLM: 输出 JSON 格式", "need_RAGAgent" in analysis_prompt)

chat_prompt = ChatLLM().generate()
check("ChatLLM: 包含课程咨询助手角色", "课程咨询助手" in chat_prompt)
check("ChatLLM: 强调 RAG 优先", "优先基于RAG" in chat_prompt)
check("ChatLLM: 包含降级策略", "一般性建议" in chat_prompt)

rag_prompt = RagLLM().generate()
check("RagLLM: 禁止编造", "禁止编造" in rag_prompt)

file_prompt = FileLLM().generate()
check("FileLLM: 临时文件检索", "临时文件" in file_prompt)


# ====================== 4. analysis_agent.py ======================
print("\n=== 4. analysis_agent.py — _check_uploaded_files ===")

from app.domain.agents.analysis_agent import AnalysisAgent
from app.domain.agents.message_bus import MessageBus

bus = MessageBus()
agent = AnalysisAgent(bus=bus)

# 4.1 基础返回值检查（legacy file_analysis/ 目录可能有文件，结果为 bool 即可）
result_no_upload = agent._check_uploaded_files()
check("无上传文件时返回 bool", isinstance(result_no_upload, bool))

# 4.2 在 UPLOAD_DIR 创建测试文件后返回 True
from core.config import settings
test_upload_dir = settings.UPLOAD_DIR
test_upload_dir.mkdir(parents=True, exist_ok=True)
test_file = test_upload_dir / "test_upload.pdf"
test_file.write_bytes(b"%PDF-1.4 test")
try:
    check("有上传文件时返回 True", agent._check_uploaded_files() is True)
finally:
    test_file.unlink(missing_ok=True)

# 4.3 在 temp 会话目录创建测试文件
temp_dir = test_upload_dir / "temp" / "1_1"
temp_dir.mkdir(parents=True, exist_ok=True)
temp_file = temp_dir / "session_doc.txt"
temp_file.write_text("test content")
try:
    check("会话临时目录有文件时返回 True", agent._check_uploaded_files() is True)
finally:
    shutil.rmtree(temp_dir, ignore_errors=True)
    # 清理空的 temp 目录
    if temp_dir.parent.is_dir() and not any(temp_dir.parent.iterdir()):
        temp_dir.parent.rmdir()


# ====================== 5. file_agent.py ======================
print("\n=== 5. file_agent.py — db=None 时仍可检索 ===")

from app.domain.agents.file_agent import FileAgent

# 5.1 db=None 构造不抛异常
try:
    fa = FileAgent(message_bus=MessageBus(), db=None, user_id=1, session_id=1)
    check("FileAgent db=None 构造成功", True)
except Exception as e:
    check(f"FileAgent db=None 构造成功 (异常: {e})", False)

# 5.2 handle() 在 db=None 时不抛异常（通过总线发送空消息测试）
fa_bus = MessageBus()
fa = FileAgent(message_bus=fa_bus, db=None, user_id=1, session_id=1)
fa_bus.publish("AnalysisAgent", "FileAgent", {"query": "测试检索", "top_k": 3})
try:
    fa.handle()
    check("FileAgent handle() db=None 不抛异常", True)
except Exception as e:
    check(f"FileAgent handle() db=None 不抛异常 (异常: {e})", False)


# ====================== 6. agent_service.py ======================
print("\n=== 6. agent_service.py — FileAgent 不再注入 shared_db ===")

import inspect
from app.application.chat.agent_service import AgentService

source_create = inspect.getsource(AgentService._create_agents)
check("FileAgent 构造不传入 shared_db", "db=shared_db" not in source_create.split("file_agent")[1].split(")")[0])


# ====================== 7. 安全机制：非关键 Agent 不走诊断 ======================
print("\n=== 7. 安全机制 — 非关键 Agent 跳过 FailureDiagnoser ===")

source_nc = inspect.getsource(AgentService._run_non_critical_agent)
check("_run_non_critical_agent: 不含 diagnoser 参数", "diagnoser" not in inspect.signature(AgentService._run_non_critical_agent).parameters)
check("_run_non_critical_agent: 不调用 diagnose()", "diagnoser.diagnose" not in source_nc and "diagnose(" not in source_nc)
check("_run_non_critical_agent: 不含 MISSING_INFO 分支", "MISSING_INFO" not in source_nc)

source_retrieval = inspect.getsource(AgentService._run_retrieval)
check("_run_retrieval: 不含 diagnoser 参数", "diagnoser" not in inspect.signature(AgentService._run_retrieval).parameters)


# ====================== 8. 安全机制：SummaryAgent 相关性回退 ======================
print("\n=== 8. 安全机制 — SummaryAgent 相关性回退 ===")

check("_run_summary_with_relevance 方法存在", hasattr(AgentService, "_run_summary_with_relevance"))

source_summary = inspect.getsource(AgentService._run_summary_with_relevance)
check("相关性回退: 调用 score_relevance", "score_relevance" in source_summary)
check("相关性回退: 阈值 0.6",
      "AGENT_SUMMARY_RELEVANCE_THRESHOLD" in source_summary
      and settings.AGENT_SUMMARY_RELEVANCE_THRESHOLD == 0.6)
check("相关性回退: 最多 3 轮", "max_rounds" in source_summary)
check("相关性回退: 超限后降级为空结果", "treating as empty" in source_summary or "fallback_output" in source_summary)
check("相关性回退: 回退时重新检索", "_run_retrieval" in source_summary)


# ====================== 9. verifier.score_relevance ======================
print("\n=== 9. verifier — score_relevance 方法 ===")

from app.domain.agents.verifier import IntentVerifier

check("IntentVerifier.score_relevance 存在", hasattr(IntentVerifier, "score_relevance"))

# 模拟 LLM 不可用时默认返回 1.0（放行）
v = IntentVerifier.__new__(IntentVerifier)
v.llm = None  # 强制让 invoke 失败
score = v.score_relevance("测试问题", "测试摘要")
check("score_relevance: LLM 异常时默认 1.0", score == 1.0)


# ====================== 汇总 ======================
print(f"\n{'='*50}")
print(f"总计: {PASS + FAIL} 项 | 通过: {PASS} | 失败: {FAIL}")
if FAIL > 0:
    print("存在失败项，请检查上方输出")
    sys.exit(1)
else:
    print("全部通过!")
