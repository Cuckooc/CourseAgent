"""
模块名：app.domain.tools.protocol

作用：
    工具调用层（P1 function calling 改造）的协议定义模块，只声明数据结构与契约，
    不含任何注册、分发与业务逻辑。全模块围绕三层边界组织：
- 决策层（RAGAgent / FileAgent）：只产出 tool_calls，不接触业务实现；
- 对接层（registry / dispatcher）：注册、鉴权、参数钳制、并发执行；
- 业务层（app/domain/tools/business/*）：纯函数 (args, ctx) -> ToolResult，承载真实业务。

安全原则：user_id / session_id / role 只允许来自服务端注入的 ToolContext，
LLM 在工具参数中传入这些字段一律忽略（dispatcher 负责剔除并告警）。

主要成员：
    - ToolContext：单次工具执行的服务端身份/范围上下文（frozen dataclass，LLM 不可触碰）；
    - SearchArgsBase / KnowledgeSearchArgs / SessionFileSearchArgs：
      工具入参的 Pydantic 模型，同时作为 llm.bind_tools 的 JSON Schema 来源，
      校验器负责 query 截断与 top_k 钳制；
    - ToolResult：业务函数统一执行结果（成功数据 / 降级空集 / 错误 / 耗时 / call_id）；
    - ToolSpec：一个工具的完整声明（名称、描述、入参模型、业务函数、归属决策层、
      风险级别、允许角色、超时、是否可降级），register_tool 的入参；
    - RISK_READ / RISK_WRITE / RISK_CONFIRM / DEFAULT_ROLES / BusinessFn：
      风险级别常量、默认允许角色与业务函数类型别名。

被谁使用：
    - app/domain/tools/registry.py：导入 ToolContext/ToolSpec 构建注册表与 StructuredTool；
    - app/domain/tools/dispatcher.py：导入 ToolContext/ToolResult/ToolSpec 做鉴权、校验与执行；
    - app/domain/tools/business/knowledge_business.py：用上述模型声明并注册
      knowledge_search / session_file_search 两个工具；
    - app/domain/agents/rag_agent.py、app/domain/agents/file_agent.py：
      仅在影子模式分支构造 ToolContext 传给 app.domain.tools.shadow.run_shadow；
    - tests/app/domain/tools/test_tool_layer_p1.py：协议边界与工具层单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from pydantic import BaseModel, field_validator

from core.config import settings


# ──────────────────────────────────────────────────────────────
# 服务端注入上下文（LLM 不可触碰）
# ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolContext:
    """单次工具执行的身份与范围上下文。

    类作用：把"这次工具调用代表谁、在哪个会话/任务中"等服务端事实显式传入
    业务函数，是业务函数获取身份信息的唯一合法来源。frozen=True 保证执行期
    不可被业务代码或 LLM 参数篡改。

    实例化位置：
        - app/domain/agents/rag_agent.py 的 RAGAgent._run_shadow()；
        - app/domain/agents/file_agent.py 的 FileAgent._run_shadow()；
          均以 Agent 自身的 user_id/session_id（来源 app/application/chat/agent_service.py
          按登录态与会话注入）、role="user"、bus.task_id 构造；
        - app/domain/tools/registry.py 的 tools_for() 接收本次请求的 ctx 并闭包注入；
        - tests/app/domain/tools/test_tool_layer_p1.py 的单测直接构造。
    关键属性去向：user_id/session_id 传入 retrieve_scoped 做数据域过滤
    （公共库 + 本人私有库 / 当前会话临时库）；role 供 dispatcher 角色鉴权；
    task_id 仅用于日志与 call_id 拼接，便于链路追踪。
    """

    # 用户 ID：来源 JWT/会话状态（SessionManager.user_id）；None 表示匿名场景
    user_id: Optional[int]
    # 会话 ID：来源 SessionManager.session_id；FileAgent 域临时库按 {user_id}_{session_id} 隔离
    session_id: Optional[int] = None
    # 角色：来源登录态，dispatcher 用它与 ToolSpec.require_roles 比对鉴权
    role: str = "user"
    # 任务 ID：来源编排层（MessageBus.task_id），用于日志追踪与 call_id 生成
    task_id: str = ""


# ──────────────────────────────────────────────────────────────
# 工具参数模型（同时作为 bind_tools 的 JSON Schema 来源）
# ──────────────────────────────────────────────────────────────
class SearchArgsBase(BaseModel):
    """检索类工具的统一入参：query + top_k。

    接口作用：所有检索类工具参数模型的基类，子类（KnowledgeSearchArgs、
    SessionFileSearchArgs）不增加字段，仅用于区分工具名与 JSON Schema。
    Pydantic 模型承担两件事：①在 dispatcher 中对 LLM 给出的原始参数做
    校验/钳制；②经 StructuredTool 自动生成 JSON Schema 提供给
    llm.bind_tools，约束 LLM 的 function calling 输出。

    钳制策略（决策容错：非法值收敛为合法值而非抛错）：
    - query 去空白并截断到 TOOL_QUERY_MAX_CHARS（默认 200，来源 core/config.py），
      防止超长 prompt 注入/拖慢 embedding；
    - top_k 钳制到 [1, TOOL_TOP_K_MAX]（默认 10，来源 core/config.py），
      非整数回退为默认值 3。
    """

    # 检索词：来源 LLM function calling 决策（经改写的用户问题），空串时业务函数直接返回空集
    query: str = ""
    # 召回条数：来源 LLM 决策，缺省 3，由 _clamp_top_k 收敛到 [1, TOOL_TOP_K_MAX]
    top_k: int = 3

    @field_validator("query", mode="before")
    @classmethod
    def _clamp_query(cls, v: Any) -> str:
        """query 预校验：None → 空串；其余去首尾空白并截断到 TOOL_QUERY_MAX_CHARS。

        被谁调用：Pydantic 在 dispatcher 执行 spec.args_model(**raw_args) 时
        自动回调（mode="before"，先于类型校验）。
        参数：v 为 LLM 给出的原始 query（可能为 None/数字等任意类型）。
        返回：规范化后的查询字符串，去向为业务函数 args.query 与向量检索。
        """
        if v is None:
            return ""
        text = str(v).strip()
        return text[: settings.TOOL_QUERY_MAX_CHARS]

    @field_validator("top_k", mode="before")
    @classmethod
    def _clamp_top_k(cls, v: Any) -> int:
        """top_k 预校验：无法转整数时回退默认 3，合法整数钳制到 [1, TOOL_TOP_K_MAX]。

        被谁调用：同 _clamp_query，由 Pydantic 在参数模型实例化时自动回调。
        参数：v 为 LLM 给出的原始 top_k（可能缺失/为字符串/越界）。
        返回：收敛后的召回条数，去向为 retrieve_scoped 的 top_k 形参。
        """
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 3
        return max(1, min(settings.TOOL_TOP_K_MAX, n))


class KnowledgeSearchArgs(SearchArgsBase):
    """knowledge_search 工具入参（公共 + 本人私有持久知识库）。

    被 app/domain/tools/business/knowledge_business.py 的 knowledge_search_fn 使用，
    随 ToolSpec 注册到 owner_agent="RAGAgent"；字段语义同 SearchArgsBase。
    """


class SessionFileSearchArgs(SearchArgsBase):
    """session_file_search 工具入参（当前会话临时知识库）。

    被 app/domain/tools/business/knowledge_business.py 的 session_file_search_fn 使用，
    随 ToolSpec 注册到 owner_agent="FileAgent"；字段语义同 SearchArgsBase。
    """


# ──────────────────────────────────────────────────────────────
# 统一执行结果
# ──────────────────────────────────────────────────────────────
@dataclass
class ToolResult:
    """业务函数执行结果（业务函数唯一允许的返回类型，类型别名 BusinessFn 已约束）。

    data 结构由各工具自行约定（检索类为 [{"content","metadata"}, ...]，
    与 MessageBus 中 SummaryAgent 现有消费契约保持一致）。
    结果去向：dispatcher 汇总后交给调用方——影子模式下由 app/domain/tools/shadow.py
    做新旧链路指纹对比；未来主链路接入后回灌 LLM 或直接组装答案，
    经 SSE 推送前端；失败/降级信息同时进入日志与 chain_log 链路记录。
    """

    # 工具名：取自 ToolSpec.name，如 "knowledge_search"
    name: str
    # 是否执行成功：T0 只读工具降级时为 False（data 为空集）；T1 写工具失败交状态机
    success: bool
    # 业务数据：检索类为 [{content, metadata}, ...]；失败时可能为 None
    data: Any = None
    # 失败原因（面向日志/排查的中文描述），成功时为 None
    error: Optional[str] = None
    # 是否降级：True 表示非关键失败被兜底为空结果，回答不中断但需人工关注
    degraded: bool = False
    # 实际执行耗时（毫秒），由 dispatcher._run_one 统一填充
    latency_ms: int = 0
    # 调用 ID："{task_id}-{序号}"，由 dispatcher 生成，用于结果排序与链路追踪
    call_id: str = ""

    @classmethod
    def empty(
        cls,
        name: str,
        degraded: bool = False,
        error: Optional[str] = None,
        call_id: str = "",
    ) -> "ToolResult":
        """构造检索类工具的空结果（成功空集 / 降级空集）。

        被谁调用：
        - app/domain/tools/business/knowledge_business.py：query 为空或会话临时库不存在时；
        - app/domain/tools/dispatcher.py：工具未注册、跨域、越权、参数非法等各类拒绝/降级分支。
        参数：
            name：工具名；degraded：是否降级（降级时 success 取 False）；
            error：降级/失败原因；call_id：调用追踪 ID。
        返回：data=[] 的 ToolResult，空列表与旧链路"非关键检索失败传空值"语义一致，
              下游 SummaryAgent 可按正常空召回处理。
        """
        return cls(name=name, success=not degraded, data=[], error=error,
                   degraded=degraded, call_id=call_id)


# ──────────────────────────────────────────────────────────────
# 工具声明
# ──────────────────────────────────────────────────────────────
# 风险级别（决定失败语义与未来确认方式）：
RISK_READ = "T0_read"      # 只读，失败可降级为空结果（不阻断回答）
RISK_WRITE = "T1_write"    # 写入，失败需上抛交状态机处理（重试/兜底）
RISK_CONFIRM = "T2_confirm"  # 外部副作用，未来需用户确认（SSE confirm 帧）

# 默认允许调用工具的角色集合；dispatcher 鉴权时要求 ctx.role 属于该元组
DEFAULT_ROLES: Tuple[str, ...] = ("user", "teacher", "admin")

# 业务函数签名：(已校验的 args 模型, ctx) -> ToolResult
# 全部实现类（业务函数）：
#   app/domain/tools/business/knowledge_business.py:
#     - knowledge_search_fn    （工具 knowledge_search，归属 RAGAgent）
#     - session_file_search_fn （工具 session_file_search，归属 FileAgent）
BusinessFn = Callable[[BaseModel, ToolContext], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整声明：LLM 展示信息 + 执行分发信息。

    接口作用：ToolSpec 是注册表里的唯一登记单元，一份声明同时驱动两条通道：
    ①name/description/args_model 经 registry.tools_for 生成 LangChain
      StructuredTool，供 llm.bind_tools 向 LLM 暴露 function calling 接口；
    ②name/business_fn/risk/require_roles/timeout_seconds/degraded 供
      ToolDispatcher 查表鉴权、钳制参数、并发执行与失败降级。
    全部实现类（当前注册的工具，均定义于 app/domain/tools/business/knowledge_business.py）：
      - knowledge_search：RAGAgent 域持久知识库检索；
      - session_file_search：FileAgent 域当前会话临时库检索。
    实例化位置：仅业务模块导入时通过 register_tool(ToolSpec(...)) 创建，
    外部不直接实例化；frozen=True 防止注册后被运行时改写。
    """

    # 工具唯一名：LLM function calling 决策出的 tool_name 必须与此精确匹配
    name: str
    # 工具描述：原样进入 JSON Schema 的 description 字段，指导 LLM 何时选择本工具
    description: str
    # 入参模型：Pydantic BaseModel 子类，dispatcher 用它校验/钳制 LLM 给出的 params
    args_model: type[BaseModel]
    # 业务函数：签名 (args_model 实例, ToolContext) -> ToolResult 的纯函数
    business_fn: BusinessFn
    # 归属决策层，如 "RAGAgent" / "FileAgent"；dispatcher 据此做跨域调用防护
    owner_agent: str
    # 风险级别：取 RISK_READ / RISK_WRITE / RISK_CONFIRM，决定失败语义
    risk: str = RISK_READ
    # 允许角色白名单：ctx.role 不在其中时 dispatcher 拒绝执行
    require_roles: Tuple[str, ...] = DEFAULT_ROLES
    # 单项执行超时（秒），缺省取 settings.TOOL_DEFAULT_TIMEOUT_SECONDS（默认 20s）
    timeout_seconds: float = settings.TOOL_DEFAULT_TIMEOUT_SECONDS
    # T0 失败/超时是否降级为空结果；False 时失败以 success=False 上抛状态机
    degraded: bool = True

    def validate(self) -> None:
        """注册时强校验：禁止假工具（业务函数不可调用直接报错）。

        被谁调用：app/domain/tools/registry.py 的 register_tool() 在写入注册表前调用，
        失败会抛 ValueError 并中止注册（导入期即暴露配置错误）。
        异常：business_fn 不可调用或 risk 不在三个合法级别常量中时抛 ValueError。
        """
        if not callable(self.business_fn):
            raise ValueError("工具 %s 的 business_fn 不可调用" % self.name)
        if self.risk not in (RISK_READ, RISK_WRITE, RISK_CONFIRM):
            raise ValueError("工具 %s 的 risk 非法: %s" % (self.name, self.risk))
