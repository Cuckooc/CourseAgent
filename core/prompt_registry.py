"""
模块名：core.prompt_registry（Prompt 版本注册表）。

作用：
    集中登记每个 Agent/Prompt 模板的语义化版本号。版本号随 chain_log
    （链路日志）一起持久化，便于事后追踪某次对话实际使用了哪个版本的 Prompt，
    支持按版本回溯效果变化、定位 Prompt 变更引发的回归。

主要成员：
    - PROMPT_VERSIONS：agent_name -> 版本号字符串的常量表（手工维护）；
    - get_prompt_version()：按 agent_name 查询版本号，未注册返回 "unknown"。

被谁使用（Grep get_prompt_version）：
    - service/agent_service.py：诊断（diagnoser）、相关性判定（relevance）
      链路写 chain_log 时记录 prompt_version；
    - app/domain/agents/state_machine.py：多智能体状态机在记录各 Agent 调用时
      写入对应 prompt_version。
"""

# Agent/Prompt 名称 -> 版本号；修改对应 Prompt 模板时需同步抬升此处版本
PROMPT_VERSIONS = {
    "rag": "1.0.0",
    "file": "1.0.0",
    "chat": "1.0.0",
    "optimize": "1.0.0",
    "information": "1.0.0",
    "predict": "1.0.0",
    "analysis": "1.0.0",
    "context_key": "1.0.0",
    "title": "1.0.0",
    "diagnoser": "1.0.0",
    "verifier": "1.0.0",
    "relevance": "1.0.0",
}


def get_prompt_version(agent_name):
    # type: (str) -> str
    """获取指定 Agent/Prompt 的版本号。

    功能：从 PROMPT_VERSIONS 常量表中查询版本号。
    被谁调用：service/agent_service.py（diagnoser、relevance 链路）、
        app/domain/agents/state_machine.py（各 Agent 调用落 chain_log 时）。
    参数：
        agent_name: Agent/Prompt 标识，来源为调用方硬编码的链路名
            （如 "diagnoser"、"relevance" 或状态机当前 agent_name）。
    返回：str；命中返回版本号（如 "1.0.0"），未注册返回 "unknown"，
        去向为 chain_log 记录中的 prompt_version 字段。
    """
    return PROMPT_VERSIONS.get(agent_name, "unknown")
