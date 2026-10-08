"""
模块名：context.py（上下文强相关判定与查询 LLM 改写）

作用：
    ContextService 依据请求上下文字段判定是否需要把业务上下文
    强制注入用户问题（注入模式）：
    - context_model：启发式判定，返回 "force"（强相关，必须改写）
      或 "similarity"（交由上层按 embedding 相似度决定）；
    - context_query：force 模式下用 model_llm.llm_business.ContextKey
      提示词调 LLM 补全问题中的模糊指代/省略；非 force 模式原样
      返回 query，不调用 LLM。

主要成员：
    - ContextService：唯一业务类，持有 embedding 模型与聊天模型。

被谁使用：
    - app/application/chat/chat_service.py：ChatService.__init__ 中实例化为
      self.context_service（约 L92）；_context_query() 在相似度
      改写路径前先调 context_model/context_query 判定与强制改写
      （文件.类.方法：chat_service.ChatService._context_query，
      调用点约 L229-L230）；改写后的查询去向 AgentService.run_agent。
      注意：本类 __init__ 构建的 embedding_model 供该类语义扩展用，
      chat_service 另有自己的 embedding 相似度计算路径。
"""
from app.infrastructure.embeddings.text_embedding import get_embedding
from typing import Dict, Any
from app.application.ports.llm import build_chat_model
from app.infrastructure.llm.llm_business import ContextKey
class ContextService:
    """上下文注入模式判定与查询改写服务。

    类作用：集中“是否强依赖业务上下文”的启发式规则，并在需要时
    调 LLM 把上下文信息改写进用户问题；无状态判定，模型成员可
    跨请求复用。
    实例化位置：app/application/chat/chat_service.py 的 ChatService.__init__
    （self.context_service = ContextService()，约 L92）。

    关键属性去向：
    - self.embedding_model：embedding.text_embedding.get_embedding()
      构建的向量化模型（相似度语义能力，供本类扩展使用）；
    - self.llm：model_llm.gateway.build_chat_model() 构建的聊天
      模型，context_query 在 force 模式下 invoke 改写查询。
    """
    def __init__(self):
        """无参构造：构建 embedding 模型与聊天网关模型两个成员。"""
        self.embedding_model = get_embedding()
        self.llm = build_chat_model()
    def context_model(self,context:Dict[str, Any]={})->str:
            """根据上下文内容判定上下文注入模式。

            功能（启发式规则，命中任一即 "force"）：
            1. 上下文为空 → "similarity"（无内容可注入）；
            2. 上下文键命中强相关关键词集合
               {年级,学科,班级,学校,userId,sessionId,课程,单元}
               → "force"（教育业务字段，问题强依赖）；
            3. 上下文不超过 3 个键且每个值字符串长度 < 50
               → "force"（短小上下文直接注入成本低、收益确定）；
            其余情况返回 "similarity"，交给 chat_service 用 embedding
            余弦相似度与 LLMConfig.SIMILARITY_THRESHOLD 决定。
            被谁调用：
            - app/application/chat/chat_service.py 的 ChatService._context_query()
              （文件.类.方法：chat_service.ChatService._context_query）；
            - 本类 context_query() 内部据此决定是否调改写 LLM。
            参数：
            - context (Dict[str, Any])：请求上下文（登录态/会话/业务
              透传字段，来源 control 层 JWT + 请求体）；默认空 dict
              为历史签名，调用方均显式传参。
            返回：
            - str："force"（强制注入并 LLM 改写）或
              "similarity"（按相似度决定）。
            """
            if not context:
                 return "similarity"
            # 强相关关键词集合（魔数）：教育场景下问题必然依赖的业务字段名
            key_words = {"年级", "学科", "班级", "学校", "userId", "sessionId", "课程", "单元"}
            for key in key_words:
                if key in context.keys():
                    # 外层已保证 key 来自 key_words，此处条件恒真：
                    # 命中即强制注入（any(...) 为历史保留的扩展判断口径）
                    if key in key_words or any(k in key for k in key_words):
                         return "force"
            # 魔数 3 / 50：字段很少且每个值都很短（<50 字符）时，
            # 上下文轻量、信息明确，直接强制注入，省掉相似度计算
            if len(context)<=3 and all(len(str(v))<50 for v in context.values()):
                return "force"
            return "similarity"
    def context_query(self,context:Dict[str, Any]={},query:str="")->str:
         """按注入模式决定是否用 LLM 改写用户查询。

         功能：context_model 判为 "force" 时，用
         model_llm.llm_business.ContextKey 生成改写提示词并调
         self.llm 补全问题中的模糊指代/省略；非 force 模式不调用
         LLM，原样返回 query（相似度判定由 chat_service 负责）。
         被谁调用：app/application/chat/chat_service.py 的
         ChatService._context_query()（文件.类.方法：
         chat_service.ChatService._context_query，约 L230）。
         参数：
         - context (Dict[str, Any])：请求上下文（来源：control 层
           JWT + 请求体并追加 history/session_id）；
         - query (str)：用户当轮原始输入（来源：请求体
           Chat.user_input）。
         返回：
         - str：force 模式返回 LLM 改写后的完整问题（兼容
           AIMessage.content 与裸字符串，均 strip）；否则原样返回
           query。去向：作为 AgentService.run_agent 的 query 参数
           进入检索与回答链路。
         """
         if self.context_model(context)=="force":

              # ContextKey 把上下文与原问题填进改写提示词（只优化问句，不答题）
              prompt=ContextKey().generate(context,query)
              response=self.llm.invoke(prompt)
              # 兼容 LangChain AIMessage（.content）与裸字符串两种返回形态
              if hasattr(response,"content"):
                   return response.content.strip()
              return str(response).strip()
         # 非 force 模式：不调用改写 LLM，原样返回（避免隐式返回 None）
         return query

    