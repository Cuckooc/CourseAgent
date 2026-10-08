"""
模块名：app.domain.agents.message_bus

作用：
Agent 消息总线。

线程安全：FastAPI 线程池下多请求并发访问，所有邮箱操作加锁。
隔离性：AgentService 每次对话创建独立 MessageBus 实例，
       避免并发请求互相窃取消息（旧实现全局单例邮箱存在串话风险）。

通信标准化：所有 Agent 间消息统一为 AgentMessage 格式，包含：
- task_id: 请求级关联 ID（同一对话请求共享）
- sender / receiver: 发送方 / 接收方 Agent 名称
- state: 当前流水线阶段（如 analyzing / retrieving）
- payload: 业务数据（原 message 字段）
- error: 错误信息（可选）

主要成员：
- AgentMessage：Agent 间标准化消息 dataclass（含 dict 式兼容访问）；
- MessageBus：发布/订阅式消息总线，提供 publish（点对点）、
  subscribe（读邮箱并清空）、broadcast（广播给全部已知邮箱）。

被谁使用（Grep 模块名结果）：
- app/application/chat/agent_service.py：AgentService._create_agents() 中
  MessageBus(task_id=...) 每请求创建一个实例，注入六个 Agent；
  编排层还直接 bus.publish("SummaryAgent", "ChatAgent", ...) 补发兜底消息；
- multi_agent 内全部 Agent：base_agent / chat_agent / vague_agent /
  analysis_agent / rag_agent / file_agent / summary_agent 均 import MessageBus
  作为构造参数类型，并通过 bus.publish / bus.subscribe 收发消息；
- tests/phase/test_all_changes.py：测试中构造总线验证 FileAgent 收消息。

典型消息流向（发布方 -> 订阅方）：
- VagueAgent -> ChatAgent（意图模糊，直接闲聊澄清）
- VagueAgent -> AnalysisAgent（意图明确，进入分析）
- AnalysisAgent -> RAGAgent / FileAgent（按 need_* 标志分发检索任务）
- RAGAgent / FileAgent -> SummaryAgent（回传检索结果 results）
- SummaryAgent -> ChatAgent（回传汇总素材 answer/keywords）
"""
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class AgentMessage:
    """Agent 间标准化消息格式（发布方产物的统一信封）。

    被谁实例化：MessageBus.publish / broadcast 内部自动包装，
    业务 Agent 无需直接构造；订阅方通过 subscribe() 收到本类实例列表。
    字段含义：
    - sender/receiver：发送方/接收方 Agent 名称；
    - payload：业务数据 dict（Agent 产物本体，如 query/results/answer）；
    - task_id：请求级关联 ID（继承自 MessageBus，便于链路日志关联）；
    - state：流水线阶段标记（可选，如 "analyzing"）；
    - error：错误信息（可选）。
    """
    sender: str
    receiver: str
    payload: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    state: str = ""
    error: str = ""

    def to_dict(self):
        # type: () -> Dict[str, Any]
        """转为字典（日志/序列化用）。被谁调用：链路日志/调试场景外部调用。"""
        return {
            "task_id": self.task_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "state": self.state,
            "payload": self.payload,
            "error": self.error,
        }

    # 向后兼容：允许 msg["message"] / msg["sender"] 式访问
    def __getitem__(self, key):
        """dict 式只读访问兼容层。

        作用：旧代码以 msg["message"] 取业务数据，此处把 "message"
        映射到新字段 payload，其余键透传同名属性；未知键抛 KeyError。
        被谁调用：各 Agent handle() 中的 msg.get("message", {})
        （get 内部转调本方法）。
        """
        if key == "message":
            # "message" 为旧字段名，等价于新结构的 payload
            return self.payload
        if key == "sender":
            return self.sender
        if key == "task_id":
            return self.task_id
        if key == "state":
            return self.state
        if key == "error":
            return self.error
        raise KeyError(key)

    def get(self, key, default=None):
        """dict 风格的 get：键不存在时返回 default，不抛 KeyError。

        被谁调用：ChatAgent._build_chain、RAGAgent.handle、FileAgent.handle、
        SummaryAgent.handle 等订阅方读取消息字段。
        """
        try:
            return self[key]
        except KeyError:
            return default


class MessageBus:
    """Agent消息总线，负责在代理之间传递消息（发布/订阅邮箱模型）。

    类作用：以"每接收方一个邮箱（list）"的方式解耦 Agent 间通信——
    发布方只写目标邮箱，订阅方读取自己的邮箱，读即清空。
    实例化位置：app/application/chat/agent_service.py 的 AgentService._create_agents()，
    每次对话请求创建一个独立实例并注入全部 Agent（并发隔离）。
    """

    def __init__(self, task_id=None):
        # type: (Optional[str]) -> None
        """初始化空总线。

        参数：task_id——请求级关联 ID；None 时自动生成 12 位 uuid 十六进制串。
        关键属性去向：mailbox 为 receiver -> 消息列表 的邮箱字典；
        task_id 会被自动盖在每条 AgentMessage 上；_lock 保护邮箱并发读写。
        """
        # 邮箱表：键为接收方 Agent 名，值为待消费的 AgentMessage 列表
        self.mailbox: Dict[str, List[AgentMessage]] = defaultdict(list)
        self.task_id = task_id or uuid.uuid4().hex[:12]
        self._lock = threading.Lock()

    def publish(self, sender: str, target_agent: str, message: dict,
                state: str = "") -> None:
        """发布消息到目标Agent（自动包装为 AgentMessage）。

        被谁调用：VagueAgent.create_agent / AnalysisAgent.create_agent /
                  RAGAgent.handle / FileAgent.handle / SummaryAgent.handle，
                  以及 agent_service.py 编排层补发 SummaryAgent 兜底消息。
        参数：
        - sender：发送方 Agent 名（来源：发送方 self.agent_name）；
        - target_agent：接收方 Agent 名（去向：其邮箱 key）；
        - message：业务 dict（发送方产物，如检索 results/汇总 answer）；
        - state：可选流水线阶段标记。
        返回：None。消息去向：target_agent 的邮箱，等待其 subscribe 消费。
        """
        # 统一包装为标准信封，自动携带本总线的 task_id
        msg = AgentMessage(
            sender=sender,
            receiver=target_agent,
            payload=message,
            task_id=self.task_id,
            state=state,
        )
        with self._lock:
            self.mailbox[target_agent].append(msg)

    def subscribe(self, agent_name: str) -> List[AgentMessage]:
        """获取当前Agent的消息列表（读取后清空）。

        被谁调用：ChatAgent._build_chain、RAGAgent.handle、
                  FileAgent.handle、SummaryAgent.handle 等消费节点入口。
        参数：agent_name——订阅（取信）方 Agent 名。
        返回：该邮箱中的 AgentMessage 列表（可能为空列表）；
        副作用：读取后该邮箱立即清空，保证每条消息只被消费一次。
        """
        with self._lock:
            messages = self.mailbox.get(agent_name, [])
            self.mailbox[agent_name] = []
            return messages

    def broadcast(self, sender: str, message: dict, state: str = "") -> None:
        """广播消息给所有Agent（向当前每个已知邮箱各投递一份副本）。

        被谁调用：当前主流程无调用方（点对点 publish 已覆盖编排需求），
                  保留用于全局通知类场景。
        参数：sender 发送方名；message 业务 dict；state 可选阶段标记。
        返回：None。注意：只投递给广播时已存在的邮箱，后注册的 Agent 收不到。
        """
        with self._lock:
            # 先快照邮箱 key，避免投递过程中字典变化
            for agent_name in list(self.mailbox.keys()):
                msg = AgentMessage(
                    sender=sender,
                    receiver=agent_name,
                    payload=message,
                    task_id=self.task_id,
                    state=state,
                )
                self.mailbox[agent_name].append(msg)
