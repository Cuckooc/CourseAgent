"""
模块名：app.domain.tools.business（工具业务实现包）

作用：
    function calling 工具层的业务层落点——每个模块承载一组工具的纯业务
    函数（签名 (args 模型, ToolContext) -> ToolResult，见 app/domain/tools/protocol.py
    的 BusinessFn），并在模块导入时显式调用 register_tool(ToolSpec(...))
    完成自注册。本 __init__ 只负责"导入子模块"这一个动作：导入即注册。

注册机制（与 app/domain/tools/registry.py 配套）：
    app/domain/tools/registry.py 的 _ensure_business_loaded() 在首次查询注册表时
    惰性 import app.domain.tools.business；本文件再 import 各业务子模块，子模块尾部的
    register_tool(...) 调用随之执行，把 ToolSpec 登记进进程级 _REGISTRY。
    该惰性链路同时规避了 registry ↔ business ↔ protocol 的循环导入。

当前成员：
    - knowledge_business：知识库检索工具
      （knowledge_search 归属 RAGAgent；session_file_search 归属 FileAgent）。

新增工具约定：
    在本包内新增模块，实现业务函数并在模块尾部 register_tool(ToolSpec(...))，
    然后在此处增加一行 import 即可；决策层 Agent 与编排层零改动。

被谁使用：
    - app/domain/tools/registry.py 的 _ensure_business_loaded() 唯一导入方
      （不允许业务主链路提前 import，以保证注册时机统一）。
"""
# noqa: F401 —— 导入只为触发 knowledge_business 模块尾部的 register_tool 自注册，
# 本命名空间不直接使用该模块名
from app.domain.tools.business import knowledge_business  # noqa: F401
