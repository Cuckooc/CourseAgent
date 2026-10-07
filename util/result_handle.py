"""
模块名：result_handle.py（Agent 对话结果的归一化拼装）

作用：
    把 AgentService.run_agent 返回的原始结果字典（含用户输入与
    chat_result 子字典）归一化为统一扁平结构，抽出 user_input、
    ai_output、context 三个字段，供上层保存对话记录与返回前端。

主要成员：
    - handle_result：模块唯一函数。

被谁使用：
    - app/application/chat/chat_service.py 的 _handle_impl()：Agent 流水线
      返回后立即调用（文件.函数：chat_service.ChatService._handle_impl）；
      归一化结果在 chat_service 内继续追加 title/user_id/session_id，
      且 ai_output 随后会被 filter_text 脱敏过滤后的 answer 覆盖，
      最终保存入库并作为 /chat/send 的 JSON 响应返回前端。
"""
from typing import Dict, Any


def handle_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """归一化 Agent 原始结果为前端/入库用的扁平字典。

    被谁调用：app/application/chat/chat_service.py 的 _handle_impl()
    （文件.函数：chat_service.ChatService._handle_impl）。
    参数：
    - result (Dict[str, Any])：数据来源为
      app/application/chat/agent_service.AgentService.run_agent 的返回值；
      可能含 "user_input"（用户本轮输入）与 "chat_result"
      （dict，Agent 最终回答挂在其 "answer" 键）。
    返回：
    - Dict[str, Any]：始终含三个键——
      user_input（原样透传，缺失时不写该键）、
      ai_output（chat_result.answer，非 dict 时为空串）、
      context（当前与 ai_output 同取 answer，作为上下文占位字段）；
      去向：chat_service 追加标题/用户/会话字段后保存对话记录，
      并返回给前端，其中 ai_output 会被 chat_service 用脱敏后的
      answer 再次覆盖。
    """
    final_result={}

    # 用户输入原样透传（键可能不存在，则不输出该字段）
    if "user_input" in result:
        final_result["user_input"] = result["user_input"]

    if "chat_result" in result and isinstance(result["chat_result"], dict):
        # ai_output 与 context 同取 answer：context 为历史保留的上下文占位字段
        final_result["ai_output"] = result["chat_result"].get("answer", "")
        final_result["context"] = result["chat_result"].get("answer", "")
    else:
        # 兜底/异常分支（Agent 返回降级结构）：统一给空串，保证前端字段不缺
        final_result["ai_output"] = ""
        final_result["context"] = ""

    return final_result