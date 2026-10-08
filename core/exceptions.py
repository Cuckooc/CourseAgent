"""
模块名：core.exceptions

作用：
    跨层共享的契约异常集中定义。分层守卫（tests/layer_guard.py）禁止
    infrastructure 反向 import app.application/domain/auth，故「实现抛出、
    业务捕获」的异常类型只能定义在横切层 core：
    - app/infrastructure/llm/gateway.py 在 LLM 全链不可用时抛出；
    - application/domain 各调用方经 app/application/ports/llm.py re-export 捕获。

主要成员：LLMUnavailableError。

被谁使用：app/infrastructure/llm/gateway.py（抛出 + re-export 旧路径）、
    app/application/ports/llm.py（re-export 给业务层）。
"""


class LLMUnavailableError(Exception):
    """主模型与全部降级模型均不可用（重试耗尽或致命错误）。"""
