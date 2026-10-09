"""
模块：chat_control.py
作用：对话相关 HTTP 接口，提供普通对话、SSE 流式对话、断网恢复与回答反馈。
主要成员：
- chat_router：对话路由对象（prefix=/chat，路由级登录用户限流 + 禁止 admin）；
- Chat / RecoverRequest / FeedbackRequest：发送对话、恢复、反馈三个请求体模型；
- _ensure_budget：LLM 预算熔断内部辅助函数（日/月 token 超限抛 429）；
- send：普通对话（非流式）；stream：SSE 流式对话；
- recover：网络中断后恢复上一轮生成结果；feedback：对 AI 回答点赞/点踩。
被谁使用：由 control/app.py 通过 `from app.api.v1.chat import chat_router` 导入并
          app.include_router 注册；路由由 HTTP 客户端（web/frontend）调用，非内部调用。
设计说明：身份一律取自 JWT，请求体中的 user_id 不再受信任。路由使用同步 def：内部
          LLM/数据库调用均为阻塞型，FastAPI 会自动放到线程池执行，避免阻塞事件循环
          （修复旧实现 async def 内直接调用同步代码的问题）。SSE 流式接口同样基于同步
          生成器：StreamingResponse 会在线程池中迭代，事件循环不被阻塞。
"""
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional

from app.application.chat.chat_service import get_chat_service
from app.application.ports.persistence import get_feedback_dao
from core.audit import audit
from app.auth.guards import get_current_user, forbid_admin
from app.auth.rate_limit import user_rate_limit
from core.responses import BizException
from core.usage import check_user_budget

# 对话路由：prefix=/chat，由 control/app.py 的 app.include_router(chat_router) 注册。
# 对话为 LLM 重调用端点：user_rate_limit(10, 60) 按登录用户限流 10 次/分钟；
# forbid_admin 守卫拒绝 admin（纯管理角色，无对话功能）。
chat_router = APIRouter(
    prefix="/chat", tags=["Chat Control"],
    dependencies=[Depends(user_rate_limit(10, 60)), Depends(forbid_admin)],
)


class Chat(BaseModel):
    """发送对话请求体模型（send 与 stream 共用）。

    实例化位置：由前端聊天界面提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 send / stream 路由的 req 参数。
    字段：
    - user_input：用户本轮输入文本，来源前端输入框；校验规则 1~4000 字符；
    - session_id：会话 ID，来源前端（0 或不传表示新建会话，由业务层分配）；无强制范围约束；
    - user_id：已废弃字段，旧客户端可能仍传，服务端忽略，身份一律以 JWT 为准。
    """

    user_input: str = Field(min_length=1, max_length=4000)
    session_id: int = 0
    # 已废弃：旧客户端可能仍传 user_id，服务端忽略并以 JWT 身份为准
    user_id: Optional[int] = None


class RecoverRequest(BaseModel):
    """断网恢复请求体模型。

    实例化位置：由前端在网络恢复后提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 recover 路由的 req 参数。
    字段：
    - session_id：待恢复的会话 ID，来源前端当前会话；默认 0，由业务层处理异常情况。
    """

    session_id: int = 0


class FeedbackRequest(BaseModel):
    """回答反馈请求体模型。

    实例化位置：由前端点赞/点踩组件提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 feedback 路由的 req 参数。
    字段：
    - session_id：反馈所属会话 ID，来源前端当前会话；必填；
    - message_index：被反馈消息在会话内的下标，来源前端；校验规则 >=0；
    - rating：评分，来源前端；必填，1=点赞，-1=点踩（路由内二次校验取值）；
    - comment：文字评论，来源前端；可选，最长 500 字符。
    """

    session_id: int
    message_index: int = Field(ge=0)
    rating: int = Field(..., description="1=点赞, -1=点踩")
    comment: Optional[str] = Field(None, max_length=500)


def _ensure_budget(user_id: int) -> None:
    """LLM 预算熔断（P1 成本治理）：日/月 token 用量达限额时拒绝新对话（429）。

    被谁调用：模块内部函数，仅由本文件的 send 与 stream 在调用 LLM 前调用。
    参数：
    - user_id：当前登录用户 ID，来源上游路由的 JWT 身份（current_user["user_id"]）。
    返回：无返回值；放行即表示预算充足。
    异常：check_user_budget 判定超限时写 llm_budget_blocked 审计事件并抛出
          BizException（HTTP 429）。限额为 0 表示不限制；/recover 不调 LLM，不做检查。
    """
    allowed, info = check_user_budget(user_id)
    if not allowed:
        audit(
            "llm_budget_blocked",
            actor={"user_id": user_id},
            result="blocked",
            daily=info.get("daily", 0),
            monthly=info.get("monthly", 0),
            daily_limit=info.get("daily_limit", 0),
            monthly_limit=info.get("monthly_limit", 0),
        )
        raise BizException(
            "本月/今日使用额度已达上限，请明日再试或联系管理员调整配额",
            http_status=429,
        )


@chat_router.post("/recover")
def recover(req: RecoverRequest, current_user: dict = Depends(get_current_user)):
    """
    网络中断恢复（不重新生成、不重复调用 LLM）：
    查看短期记忆（当前会话已生成的对话，含中断前已生成内容），不查长期记忆。

    HTTP 方法+路径：POST /chat/recover。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 10 次/分钟限流、
                forbid_admin 禁止 admin 访问；本接口不调 LLM，故不做预算检查。
    被谁调用：由 HTTP 客户端（web/frontend 网络恢复逻辑）调用，非内部调用。
    参数：
    - req：恢复请求体，RecoverRequest 模型，session_id 来自前端当前会话；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：HTTP JSON 响应 {status:"success", data: {...}}；data.recover_status：
          completed=上一轮已完整生成（附 user_content/ai_output，前端比对 user_content
          与等待中的提问一致才回补气泡）；missing=该轮未生成完成或会话记录已过期
          （附 message，前端提示重新发送）。该字段由内部 status 改名而来，避免与外层
          传输状态 status 重名。
    """
    chat_service = get_chat_service()
    result = chat_service.recover(current_user["user_id"], req.session_id)
    # data.recover_status: completed=上一轮已完成（附 user_content/ai_output）；
    # missing=未完成（附 message），避免与外层传输状态 status 重名
    result["recover_status"] = result.pop("status")
    return {"status": "success", "data": result}


@chat_router.post("/feedback")
def feedback(req: FeedbackRequest, current_user: dict = Depends(get_current_user)):
    """用户反馈：对 AI 回答点赞或点踩，供后续分析优化。

    HTTP 方法+路径：POST /chat/feedback。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 10 次/分钟限流、
                forbid_admin 禁止 admin 访问。
    被谁调用：由 HTTP 客户端（web/frontend 反馈组件）调用，非内部调用。
    参数：
    - req：反馈请求体，FeedbackRequest 模型，含 session_id/message_index/rating/comment，均来自前端；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：成功时返回 {status:"success"} 的 HTTP JSON 响应。
    异常：rating 非 1/-1 时抛 BizException(400)；反馈落库失败时抛 BizException（默认 400 语义）。
    """
    if req.rating not in (1, -1):
        raise BizException("rating 必须为 1（点赞）或 -1（点踩）")
    dao = get_feedback_dao()
    ok = dao.insert(
        user_id=current_user["user_id"],
        session_id=req.session_id,
        message_index=req.message_index,
        rating=req.rating,
        comment=req.comment,
    )
    if not ok:
        raise BizException("反馈保存失败，请稍后重试")
    return {"status": "success"}


@chat_router.post("/send")
def send(req: Chat, current_user: dict = Depends(get_current_user)):
    """普通对话接口（非流式）：预算检查后交由聊天服务一次性生成完整回答。

    HTTP 方法+路径：POST /chat/send。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 10 次/分钟限流、
                forbid_admin 禁止 admin 访问。
    被谁调用：由 HTTP 客户端（web/frontend 聊天界面）调用，非内部调用。
    参数：
    - req：对话请求体，Chat 模型，user_input/session_id 来自前端（user_id 字段废弃忽略）；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：chat_service.handle 的结果 dict，直接作为 HTTP JSON 响应给前端（含会话与回答内容）。
    异常：预算超限时 _ensure_budget 抛 BizException(429)。
    """
    _ensure_budget(current_user["user_id"])
    chat_service = get_chat_service()
    # 组装上游上下文：身份与会话号均以 JWT/请求模型为准，供 service 层检索与落库
    context = {
        "user_id": current_user["user_id"],
        "session_id": req.session_id,
    }
    return chat_service.handle(req.user_input, context)


@chat_router.post("/stream")
def stream(req: Chat, current_user: dict = Depends(get_current_user)):
    """
    SSE 流式对话（text/event-stream）。

    HTTP 方法+路径：POST /chat/stream。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 10 次/分钟限流、
                forbid_admin 禁止 admin 访问。预算熔断在响应头发出前完成。
    被谁调用：由 HTTP 客户端（web/frontend 聊天界面 EventSource/fetch 流式读取）调用，非内部调用。
    参数：
    - req：对话请求体，Chat 模型，user_input/session_id 来自前端（user_id 字段废弃忽略）；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：StreamingResponse（media_type=text/event-stream; charset=utf-8），
          由内部同步生成器 event_gen 在线程池中迭代产出，不阻塞事件循环；
          响应头关闭缓存并设置 X-Accel-Buffering: no 防止代理缓冲。
    异常：预算超限时 _ensure_budget 在流开始前抛 BizException(429)（而非流内 error 帧）。
    帧格式：data: {"type": "status"|"delta"|"done"|"error", ...}\n\n
    - status: {"type": "status", "stage": str, "message": str}（前置阶段提示，首帧 <1s）
    - delta:  {"type": "delta", "content": "文本增量"}
    - done:   {"type": "done", "session_id": int, "title": str, "ai_output": str}
              长对话触发自动滚换时额外携带 "rolled_over": true 与
              "previous_session_id": int，此时 session_id 为接续的新会话 id
              （最近 N 轮原文/早期摘要/关键词/会话临时知识库已迁移）。
    - error:  {"type": "error", "message": str}
    """
    # 预算熔断在响应头发出前判定：超限时直接返回 429 而非流内 error 帧
    _ensure_budget(current_user["user_id"])
    chat_service = get_chat_service()
    # 组装上游上下文：身份与会话号均以 JWT/请求模型为准
    context = {
        "user_id": current_user["user_id"],
        "session_id": req.session_id,
    }

    def event_gen():
        """SSE 帧生成器（内部闭包）：把 chat_service 产出的帧 dict 序列化为 SSE data 行。

        被谁调用：非显式调用，作为 StreamingResponse 的可迭代体由 FastAPI/线程池逐帧迭代。
        产出：每个 frame 输出一行 `data: <json>\\n\\n`（ensure_ascii=False 保留中文）。
        """
        for frame in chat_service.handle_stream(req.user_input, context):
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
