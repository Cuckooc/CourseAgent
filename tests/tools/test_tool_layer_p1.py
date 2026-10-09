"""
模块名：tests/app/domain/tools/test_tool_layer_p1.py。

P1 工具调用层单元测试套件（无网络 / 无 LLM / 无 embedding / 无数据库依赖），
风险类型：工具注册表越权、参数越界、Dispatcher 横切缺口（未知工具/跨域/角色/
身份注入/重复调用/超时）、业务函数存储旁路错误。

测试函数清单（模块级 pytest 函数，无测试类）：
- 注册表：test_registry_tools_and_owners（工具归属与 tools_for 决策层隔离）
- 参数钳制：test_args_clamping（query 截断 200、top_k 边界钳制）
- Dispatcher 横切（依赖 echo_tool 夹具）：test_dispatcher_unknown_tool、
  test_dispatcher_cross_owner_denied、test_dispatcher_role_denied、
  test_dispatcher_identity_injection_stripped、test_dispatcher_dedup、
  test_dispatcher_timeout_degrades
- 业务函数（monkeypatch 替身，零网络）：test_knowledge_search_business、
  test_knowledge_search_empty_query、test_session_file_search_business、
  test_session_file_search_no_session
夹具：echo_tool（向 app.domain.tools.registry._REGISTRY 临时注册 ut_echo/ut_slow 两个
ToolSpec，yield 后 pop 清理）。

被测对象来源：
- app/domain/tools/registry.py（get_spec/tools_for/_REGISTRY 注册表）；
- app/domain/tools/dispatcher.py（get_tool_dispatcher：归属/角色校验、身份字段剥离、
  去重、超时降级为 ToolResult.degraded）；
- app/domain/tools/protocol.py（ToolSpec/ToolContext/ToolResult/KnowledgeSearchArgs、
  RISK_READ/RISK_WRITE）；
- app/domain/tools/business.py（knowledge_search_fn/session_file_search_fn，
  retrieve_scoped/get_persistent_db/get_temp_store 三个外部依赖被替身）。

运行方式：
    pytest tests/app/domain/tools/test_tool_layer_p1.py
    # 无自定义 marker、无需后端/DB/网络；纯进程内单测，可直接收集运行。
"""
import time

import pytest
from langchain_core.documents import Document

from app.domain.tools.protocol import (
    KnowledgeSearchArgs,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.domain.tools import registry as registry_mod
from app.domain.tools.dispatcher import get_tool_dispatcher
from app.domain.tools.business import knowledge_business


# ──────────────────────────────────────────────────────────────
# 注册表
# ──────────────────────────────────────────────────────────────
def test_registry_tools_and_owners():
    """目的：验证工具注册表归属与按决策层隔离。

    前置：tools 包正常导入（业务工具自动注册）。
    数据来源与意图：正常用例——取 knowledge_search/session_file_search 的
    ToolSpec，再分别以 RAGAgent/FileAgent/NotExistAgent 调 tools_for。
    预期断言：owner_agent 分别为 RAGAgent/FileAgent；tools_for 只返回本层工具；
    未知 agent 返回空列表。无清理。
    """
    spec_k = registry_mod.get_spec("knowledge_search")
    spec_f = registry_mod.get_spec("session_file_search")
    assert spec_k is not None and spec_f is not None
    assert spec_k.owner_agent == "RAGAgent"
    assert spec_f.owner_agent == "FileAgent"

    ctx = ToolContext(user_id=1, session_id=2)
    rag_tools = registry_mod.tools_for("RAGAgent", ctx)
    file_tools = registry_mod.tools_for("FileAgent", ctx)
    assert [t.name for t in rag_tools] == ["knowledge_search"]
    assert [t.name for t in file_tools] == ["session_file_search"]
    assert registry_mod.tools_for("NotExistAgent", ctx) == []


# ──────────────────────────────────────────────────────────────
# 参数钳制
# ──────────────────────────────────────────────────────────────
def test_args_clamping():
    """目的：验证 KnowledgeSearchArgs 的 Pydantic 边界钳制。

    数据来源与意图（边界）：300 字符空白填充 query（超 200 上限）+top_k=999
    （超 10 上限）；top_k=0（低于下限 1）；query=None、top_k="abc"（非法类型）。
    预期断言：query 截断为 200、top_k 999→10、0→1、非法值回落默认 3、None→""。
    """
    a = KnowledgeSearchArgs(query="  " + "x" * 300, top_k=999)
    assert len(a.query) == 200
    assert a.top_k == 10

    a2 = KnowledgeSearchArgs(query="q", top_k=0)
    assert a2.top_k == 1

    a3 = KnowledgeSearchArgs(query=None, top_k="abc")
    assert a3.query == ""
    assert a3.top_k == 3


# ──────────────────────────────────────────────────────────────
# Dispatcher：用临时注册的 echo 工具验证横切能力
# ──────────────────────────────────────────────────────────────
@pytest.fixture
def echo_tool():
    """夹具：向全局 app.domain.tools.registry._REGISTRY 临时注册两个测试 ToolSpec。

    注册内容：ut_echo（回显 text 与 ctx.user_id，timeout 5s）与
    ut_slow（sleep 1s，timeout 0.2s，用于超时降级）；同时把
    _business_loaded 置 True 防止业务加载覆盖测试注册。
    数据来源：EchoArgs 为夹具内局部 pydantic 模型。
    yield 去向：返回 (echo_spec, slow_spec) 供 Dispatcher 横切用例按名调用。
    清理：yield 后从 _REGISTRY pop 两个键（不恢复 _business_loaded）。
    消费者：test_dispatcher_cross_owner_denied/role_denied/
    identity_injection_stripped/dedup/timeout_degrades。
    """
    from pydantic import BaseModel

    class EchoArgs(BaseModel):
        text: str = ""

    def echo_fn(args, ctx):
        return ToolResult(name="ut_echo", success=True,
                          data={"text": args.text, "uid": ctx.user_id})

    def slow_fn(args, ctx):
        time.sleep(1.0)
        return ToolResult(name="ut_slow", success=True, data="ok")

    echo = ToolSpec(name="ut_echo", description="echo", args_model=EchoArgs,
                    business_fn=echo_fn, owner_agent="UTAgent",
                    timeout_seconds=5)
    slow = ToolSpec(name="ut_slow", description="slow", args_model=EchoArgs,
                    business_fn=slow_fn, owner_agent="UTAgent",
                    timeout_seconds=0.2)
    registry_mod._REGISTRY["ut_echo"] = echo
    registry_mod._REGISTRY["ut_slow"] = slow
    registry_mod._business_loaded = True
    yield echo, slow
    registry_mod._REGISTRY.pop("ut_echo", None)
    registry_mod._REGISTRY.pop("ut_slow", None)


def test_dispatcher_unknown_tool():
    """目的：Dispatcher 对未注册工具的降级处理（不抛异常）。

    数据意图（异常输入）：调用名 not_exist。
    预期断言：返回恰 1 个 ToolResult，degraded=True、success=False。
    """
    results = get_tool_dispatcher().execute(
        [{"name": "not_exist", "args": {}}], ToolContext(user_id=1))
    assert len(results) == 1
    assert results[0].degraded and not results[0].success


def test_dispatcher_cross_owner_denied(echo_tool):
    """目的：跨决策层（owner）调用他层工具必须被拒绝。

    数据意图（越权）：FileAgent 身份请求 RAGAgent 所属的 knowledge_search。
    预期断言：结果 degraded 且 error 含「不属于」归属提示。
    """
    results = get_tool_dispatcher().execute(
        [{"name": "knowledge_search", "args": {"query": "x"}}],
        ToolContext(user_id=1), owner_agent="FileAgent")
    assert results[0].degraded
    assert "不属于" in results[0].error


def test_dispatcher_role_denied(echo_tool):
    """目的：角色不满足 ToolSpec 风险级别要求时拒绝。

    前置：echo_tool 夹具已注册 ut_echo（UTAgent 层）。
    数据意图（越权）：ToolContext(role="guest") 调本层工具。
    预期断言：degraded=True 且 error 含「角色」。
    """
    results = get_tool_dispatcher().execute(
        [{"name": "ut_echo", "args": {"text": "hi"}}],
        ToolContext(user_id=1, role="guest"), owner_agent="UTAgent")
    assert results[0].degraded and "角色" in results[0].error


def test_dispatcher_identity_injection_stripped(echo_tool):
    """目的：防止 LLM 在 args 中注入身份字段提权。

    数据意图（恶意）：args 同时带 user_id=999/session_id=888/role="admin"，
    而真实 ToolContext 为 uid=1/sid=2/role=user。
    预期断言：调用成功，但 echo 业务函数读到的 ctx.user_id 仍为 1
    （Dispatcher 必须剥离 args 内身份键，只信服务端 ToolContext）。
    """
    results = get_tool_dispatcher().execute(
        [{"name": "ut_echo",
          "args": {"text": "hi", "user_id": 999, "session_id": 888, "role": "admin"}}],
        ToolContext(user_id=1, session_id=2, role="user"),
        owner_agent="UTAgent")
    assert results[0].success
    # 业务函数只看得到 ctx 中的真实身份
    assert results[0].data["uid"] == 1


def test_dispatcher_dedup(echo_tool):
    """目的：同一批次内重复工具调用被去重，避免重复副作用与浪费。

    数据意图（边界/重复）：同一 ut_echo 相同 args 连发 3 次。
    预期断言：只执行并返回 1 个结果。
    """
    calls = [{"name": "ut_echo", "args": {"text": "same"}}] * 3
    results = get_tool_dispatcher().execute(
        calls, ToolContext(user_id=1), owner_agent="UTAgent")
    assert len(results) == 1


def test_dispatcher_timeout_degrades(echo_tool):
    """目的：业务函数超过 ToolSpec.timeout_seconds 时降级而非抛异常。

    前置：ut_slow 睡眠 1s 而超时阈值仅 0.2s。
    预期断言：degraded=True 且 error 含「超时」。
    """
    results = get_tool_dispatcher().execute(
        [{"name": "ut_slow", "args": {"text": "x"}}],
        ToolContext(user_id=1), owner_agent="UTAgent")
    assert results[0].degraded
    assert "超时" in results[0].error


# ──────────────────────────────────────────────────────────────
# 业务函数（mock 存储与检索，零网络）
# ──────────────────────────────────────────────────────────────
def test_knowledge_search_business(monkeypatch):
    """目的：knowledge_search_fn 正常检索链路与输出字段映射。

    替身外部依赖（monkeypatch）：knowledge_business.get_persistent_db
    返回 "FAKE_DB" 占位；retrieve_scoped 换为 fake_retrieve 并捕获入参、
    返回 1 个含 output 元数据的 Document（零网络/零向量库）。
    数据意图（正常）：query="python 路径"、uid=42/sid=7。
    预期断言：success；content 取 metadata["output"]；RAG 域不传 session_id
    （captured session_id is None）且 user_id 透传 42。无清理（monkeypatch 自动还原）。
    """
    fake_docs = [Document(page_content="正文", metadata={"output": "output字段正文"})]

    monkeypatch.setattr(knowledge_business, "get_persistent_db",
                        lambda: "FAKE_DB")
    captured = {}

    def fake_retrieve(db, query, top_k, user_id, session_id):
        captured.update(db=db, query=query, top_k=top_k,
                        user_id=user_id, session_id=session_id)
        return fake_docs

    monkeypatch.setattr(knowledge_business, "retrieve_scoped", fake_retrieve)

    result = knowledge_business.knowledge_search_fn(
        KnowledgeSearchArgs(query="python 路径"), ToolContext(user_id=42, session_id=7))
    assert result.success
    assert result.data[0]["content"] == "output字段正文"
    # RAG 域不传 session_id，临时库不并入本路
    assert captured["session_id"] is None
    assert captured["user_id"] == 42


def test_knowledge_search_empty_query():
    """目的：空 query 走快速返回，不触碰检索依赖。

    数据意图（边界）：KnowledgeSearchArgs(query="")。
    预期断言：success 且 data 为空列表（无 monkeypatch 也不报错，证明未调检索）。
    """
    result = knowledge_business.knowledge_search_fn(
        KnowledgeSearchArgs(query=""), ToolContext(user_id=1))
    assert result.success and result.data == []


def test_session_file_search_business(monkeypatch):
    """目的：session_file_search_fn 走会话临时库旁路且主 db 必须为 None。

    替身外部依赖（monkeypatch）：get_temp_store 返回 FakeStore
    （has_session=True、get_db 返回 "TEMP_DB"）；retrieve_scoped 换为
    fake_retrieve 捕获 (db, uid, sid) 并返回 1 个上传文件 Document。
    数据意图（正常）：uid=42/sid=7 检索「资料里说了什么」。
    预期断言：success 且 content 为文件内容；captured 恰为
    {"db": None, "uid": 42, "sid": 7}——临时库由 retrieve_scoped 按
    session_id 旁路加载，传同实例会让旁路短路、scope 过滤后召回为 0。
    """
    class FakeStore:
        def has_session(self, uid, sid):
            return True

        def get_db(self, uid, sid):
            return "TEMP_DB"

    monkeypatch.setattr(knowledge_business, "get_temp_store", lambda: FakeStore())

    captured = {}

    def fake_retrieve(db, query, top_k, user_id, session_id):
        captured.update(db=db, uid=user_id, sid=session_id)
        return [Document(page_content="上传文件内容", metadata={"source": "a.txt"})]

    monkeypatch.setattr(knowledge_business, "retrieve_scoped", fake_retrieve)

    result = knowledge_business.session_file_search_fn(
        KnowledgeSearchArgs(query="资料里说了什么"),
        ToolContext(user_id=42, session_id=7))
    assert result.success
    assert result.data[0]["content"] == "上传文件内容"
    # db 必须为 None：临时库由 retrieve_scoped 按 session_id 旁路加载，
    # 传入 temp_db 会导致旁路因同实例判断被跳过、scope 过滤后召回为 0
    assert captured == {"db": None, "uid": 42, "sid": 7}


def test_session_file_search_no_session(monkeypatch):
    """目的：无会话上下文 / 会话无上传两种场景都安全返回空。

    数据意图（边界）：先不传 session_id（不应触碰存储）；再用 monkeypatch
    替身 get_temp_store 为 has_session=False 的 FakeStore（sid=9 无上传）。
    预期断言：两次均 success 且 data==[]。
    """
    # 无会话上下文 → 空结果，不触碰存储
    result = knowledge_business.session_file_search_fn(
        KnowledgeSearchArgs(query="q"), ToolContext(user_id=42))
    assert result.success and result.data == []

    # 会话无上传 → 空结果
    class FakeStore:
        def has_session(self, uid, sid):
            return False

    monkeypatch.setattr(knowledge_business, "get_temp_store", lambda: FakeStore())
    result2 = knowledge_business.session_file_search_fn(
        KnowledgeSearchArgs(query="q"), ToolContext(user_id=42, session_id=9))
    assert result2.success and result2.data == []
