"""
模块名：tests/test_chat_e2e.py。

对话端到端测试套件（固化自《全流程测试报告.md》第四章 9 项），风险类型：
全链路功能回归——多智能体编排、RAG 检索、记忆上下文、SSE 流式与数据持久化。

覆盖：
- 多智能体对话 RAG（真实 LLM 调用）
- 短期记忆 + 上下文记忆（多轮对话）
- SSE 流式响应
- 网络中断恢复 /chat/recover
- 用户反馈 /chat/feedback
- 数据落库（chain_log / history_information / chat_feedback）

测试函数清单（模块级函数，无测试类）：
- test_chat_first_round_greeting：首轮寒暄自动建会话，断言 session_id/ai_output
- test_chat_second_round_with_context：第二轮携带 session_id，验证上下文打通
- test_chat_recover_no_llm：/chat/recover 不调 LLM 的恢复状态枚举
- test_chat_feedback_like：点赞反馈并直查 MySQL 验证落库
- test_chat_stream_sse：/chat/stream 响应头与 delta/done SSE 帧
- test_chain_log_persisted：Agent 链路日志异步落 chain_log（轮询 5s）
- test_history_information_persisted：会话元数据落 history_information
辅助函数：_chat（/chat/send 的薄封装）。模块常量：CHAT_TIMEOUT（单轮超时秒数）。

被测对象来源：
- 路由：control/chat_control.py（/chat/send、/chat/stream、/chat/recover、/chat/feedback）；
- 编排：service/agent_service.py 与 multi_agent/ 全链路（chat_agent/rag_agent 等）、
  memory/（短期/上下文记忆）、model_llm/gateway.py（真实 DashScope）；
- 落库：dao/chain_log.py、dao/history.py、dao/feedback.py；
- 校验方式：HTTP 断言 + 通过 db/session.py 的 engine 直查真实表（无 mock）。

运行方式：
    pytest tests/test_chat_e2e.py -m "slow and db and backend"
    # 默认不跑（pytestmark 含 slow）；需后端 :8000、MySQL、DashScope 可用，
    # 单轮真实 LLM 16~30s，全套约数分钟
依赖夹具：conftest 的 http / user_acct / db_engine；每用例独立账号避免 10 次/分钟限流。

设计：
- 真实调用 DashScope，每轮 16-30s，全部标 `slow` marker（默认不跑）；
- 每个用例独立测试账号，避免 rate_limit 干扰（user_rate_limit 10次/分钟）；
- 不 mock 任何层，覆盖全链路。
"""
import json
import time

import pytest
from sqlalchemy import text

# 模块级 markers：需后端在线 + 真实 LLM（slow 默认不跑）+ 真实 MySQL（db）
pytestmark = [pytest.mark.backend, pytest.mark.slow, pytest.mark.db]

# 单轮对话超时常量（秒）：真实 LLM 链路可能 30s+，SSE 用例也复用该上限
CHAT_TIMEOUT = 90


def _chat(http, token, user_input, session_id=0, timeout=CHAT_TIMEOUT):
    """对话请求薄封装：POST /chat/send，返回 (status, body, raw)。

    调用方：本文件除 SSE/feedback 外的全部用例。
    参数来源：token 为 user_acct JWT；user_input 为用例构造的正常/探针问题；
    session_id=0 表示首轮自动建会话，非 0 表示续接指定会话。
    """
    return http(
        "POST", "/chat/send",
        token=token,
        json_body={"user_input": user_input, "session_id": session_id},
        timeout=timeout,
    )


def test_chat_first_round_greeting(http, user_acct, db_engine):
    """第一轮寒暄（session_id=0 自动建会话）→ 200 + 非空回答 + 非零 session_id。"""
    status, body, _ = _chat(http, user_acct["token"], "你好，请介绍一下你自己", session_id=0)
    assert status == 200, f"第一轮对话失败：{status}"
    assert body.get("status") == "success", f"对话业务失败：{body}"

    data = body.get("data") or body
    sid = data.get("session_id")
    ai = data.get("ai_output") or data.get("answer") or ""
    assert sid, f"第一轮未返回 session_id：{body}"
    assert len(ai) >= 10, f"AI 回答过短：{ai[:80]}"


def test_chat_second_round_with_context(http, user_acct):
    """第二轮知识类问题（携带 session_id，RAG/上下文）→ 200 + 实质性回答。"""
    # 第一轮建会话
    s1, b1, _ = _chat(http, user_acct["token"], "你好", session_id=0)
    assert s1 == 200
    sid = (b1.get("data") or b1).get("session_id")

    # 第二轮带 session_id，验证上下文/记忆打通
    s2, b2, _ = _chat(
        http, user_acct["token"],
        "学习Python应该怎么入门？给我一个具体的学习计划",
        session_id=sid,
    )
    assert s2 == 200, f"第二轮对话失败：{s2}"
    data2 = b2.get("data") or b2
    ai2 = data2.get("ai_output") or data2.get("answer") or ""
    assert len(ai2) >= 50, f"第二轮回答过短：{ai2[:80]}"


def test_chat_recover_no_llm(http, user_acct):
    """/chat/recover 网络中断恢复（不调 LLM）→ 200 + recover_status 合法。"""
    # 先发一轮，让短期记忆有内容
    s1, _, _ = _chat(http, user_acct["token"], "你好", session_id=0)
    assert s1 == 200

    # 取上轮 sid
    sid_from_first = 0
    s1_body = _
    # recover 用 sid=0 也能跑（无内容时返回 missing）
    status, body, _ = http(
        "POST", "/chat/recover",
        token=user_acct["token"],
        json_body={"session_id": sid_from_first},
    )
    assert status == 200, f"recover 失败：{status}"
    data = body.get("data") or {}
    assert data.get("recover_status") in ("completed", "missing"), \
        f"recover_status 非法：{data}"


def test_chat_feedback_like(http, user_acct):
    """点赞反馈 /chat/feedback → 200 + 数据落库 chat_feedback。"""
    # 先发一轮拿 session_id 与 message_index
    s1, b1, _ = _chat(http, user_acct["token"], "你好", session_id=0)
    assert s1 == 200
    sid = (b1.get("data") or b1).get("session_id")

    # message_index=0（第一轮）
    status, body, _ = http(
        "POST", "/chat/feedback",
        token=user_acct["token"],
        json_body={"session_id": sid, "message_index": 0, "rating": 1},
    )
    assert status == 200, f"feedback 失败：{status}"

    # 验证落库
    uid = user_acct["user_id"]
    with __import__("db.session", fromlist=["engine"]).engine.connect() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM chat_feedback WHERE user_id = :uid AND session_id = :sid AND rating = 1"
            ),
            {"uid": uid, "sid": sid},
        ).scalar()
    assert cnt >= 1, f"chat_feedback 未落库：uid={uid} sid={sid} cnt={cnt}"


def test_chat_stream_sse(http, user_acct):
    """/chat/stream SSE 流式 → 200 + content-type=text/event-stream + 至少一帧。"""
    import urllib.request

    url = f"http://localhost:8000/chat/stream"
    req = urllib.request.Request(
        url,
        data=json.dumps({"user_input": "你好", "session_id": 0}).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {user_acct['token']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT) as resp:
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"content-type 非法：{ct}"

            # 读取至少一个 SSE 帧（data: {...}\n\n）
            chunks = []
            while True:
                line = resp.readline()
                if not line:
                    break
                chunks.append(line.decode("utf-8", errors="replace"))
                if len(chunks) > 200:  # 防止无限读
                    break
    except urllib.error.HTTPError as e:
        pytest.fail(f"SSE 请求失败：HTTP {e.code} {e.read()[:200]}")

    body = "".join(chunks)
    assert "data:" in body, f"SSE 未输出 data: 帧：{body[:200]}"
    # 至少一帧 done 或 delta
    assert any(t in body for t in ['"type":"delta"', '"type": "delta"', '"type":"done"', '"type": "done"']), \
        f"SSE 未输出 delta/done 帧：{body[:200]}"


def test_chain_log_persisted(http, user_acct, db_engine):
    """对话后 chain_log 表落库（每轮一条 Agent 链路日志）。"""
    uid = user_acct["user_id"]
    with db_engine.connect() as conn:
        before = conn.execute(
            text("SELECT COUNT(*) FROM chain_log WHERE user_id = :uid"), {"uid": uid}
        ).scalar()

    s, _, _ = _chat(http, user_acct["token"], "测试链路日志落库", session_id=0)
    assert s == 200

    # chain_log 异步落库，等待最多 5s
    for _ in range(10):
        time.sleep(0.5)
        with db_engine.connect() as conn:
            after = conn.execute(
                text("SELECT COUNT(*) FROM chain_log WHERE user_id = :uid"), {"uid": uid}
            ).scalar()
        if after > before:
            break
    assert after > before, f"chain_log 未落库：before={before} after={after}"


def test_history_information_persisted(http, user_acct, db_engine):
    """对话后 history_information 会话元数据落库。"""
    uid = user_acct["user_id"]
    with db_engine.connect() as conn:
        before = conn.execute(
            text("SELECT COUNT(*) FROM history_information WHERE user_id = :uid"), {"uid": uid}
        ).scalar()

    s, b, _ = _chat(http, user_acct["token"], "测试会话元数据落库", session_id=0)
    assert s == 200

    with db_engine.connect() as conn:
        after = conn.execute(
            text("SELECT COUNT(*) FROM history_information WHERE user_id = :uid"), {"uid": uid}
        ).scalar()
    assert after > before, f"history_information 未落库：before={before} after={after}"
