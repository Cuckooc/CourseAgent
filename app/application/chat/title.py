"""
模块名：title.py（会话标题生成：LLM 提示词链封装）

作用：
    Title 继承 model_llm.llm_business.TitleLLM（后者提供标题提示词
    模板与 self.llm 聊天模型），把「模板 → PromptTemplate →
    LLM 链调用 → 20 字截断」组装为一个 title() 调用，依据首轮
    上下文与用户问题提炼会话标题；LLM 调用失败时兜底返回“会话”。

主要成员：
    - Title：TitleLLM 的唯一子类，title() 为唯一业务方法。

被谁使用：
    - app/application/chat/chat_service.py：ChatService.__init__ 中实例化为
      self.title_service（约 L93），由标题预取线程执行体
      _handle_title() 调用 self.title_service.title(context,
      user_input)（文件.类.方法：chat_service.ChatService._handle_title）；
      生成标题随对话结果保存入库并回传前端，异常时 chat_service
      另有“新会话”兜底。
"""
from app.infrastructure.llm.llm_business import TitleLLM
from typing import Dict, Any
from langchain_core.prompts import PromptTemplate

class Title(TitleLLM):
    """会话标题生成器（TitleLLM 提示词模板的可调用封装）。

    类作用：复用父类 TitleLLM 的 generate() 模板与 __init__ 中构建的
    self.llm 聊天模型，仅新增 title() 方法完成链式调用与截断兜底，
    自身不引入新属性。
    实例化位置：app/application/chat/chat_service.py 的 ChatService.__init__
    （self.title_service = Title()，约 L93）。
    """

    def __init__(self):
        """无参构造：完全委托父类 TitleLLM.__init__（构建 self.llm）。"""
        super().__init__()

    def title(self, context_text:Dict[str,Any], query: str)-> str:
        """调用 LLM 依据上下文与用户问题生成会话标题。

        被谁调用：app/application/chat/chat_service.py 的 ChatService._handle_title()
        （标题预取线程池 worker，文件.类.方法：
        chat_service.ChatService._handle_title）。
        参数：
        - context_text (Dict[str, Any])：请求上下文 dict（含
          session_id/history 等，来源：control 层 JWT + 请求体组装，
          填入提示词的 {context_text} 占位符）；
        - query (str)：用户本轮原始输入（标题只依赖问题不依赖回答，
          来源：请求体 Chat.user_input，填入 {query} 占位符）。
        返回：
        - str：LLM 生成的标题，strip 后超过魔数 20 字则截断前 20 字
          （与提示词“20 字以内”要求双保险）；LLM 调用任意异常时
          打印失败信息并兜底返回“会话”，保证标题流程不阻断主链路。
          去向：chat_service 写入 final_result["title"]，随对话
          记录持久化并回传前端。
        """
        # 父类 generate() 给出带 {query}/{context_text} 占位符的标题模板
        template= super().generate()
        prompt=PromptTemplate.from_template(template)
        # LCEL 链：模板填充后交给继承自 TitleLLM 的聊天模型
        chain=prompt | self.llm
        try:
            result = chain.invoke({
                "query": query,
                "context_text": context_text
            })
            # 兼容 LangChain AIMessage（.content）与裸字符串两种返回形态
            title = result.content if hasattr(result, 'content') else str(result)
            # 魔数 20：标题最大长度，超长硬切前 20 字
            title = title.strip()[:20] if len(title) > 20 else title
            return title
        except Exception as e:
            print(f"  ✗ 获取标题失败: {e}")
            return "会话"
