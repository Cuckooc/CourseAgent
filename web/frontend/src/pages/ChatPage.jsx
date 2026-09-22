/**
 * @文件 ChatPage.jsx
 * @作用 对话咨询主界面：左侧会话列表（切换 = 加载持久化记录）+ 右侧消息流与输入区；
 * 挂载时拉取会话列表；currentSessionId 变化（含刷新后恢复）即加载对应记录；
 * 监听长对话自动滚换通知并弹出轻提示
 * @主要成员 ChatPage（默认导出，页面组件）
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /chat；
 * 允许角色 user/teacher（src/router/nav-config.jsx 的 MAIN_NAV），
 * 外层经 RequireAuth 登录守卫与 RequirePermission 角色守卫；页面渲染在 AppLayout 的 <Outlet/> 中
 */
import { useEffect } from 'react';
import { App } from 'antd';
import ChatSidebar from '../components/chat/ChatSidebar.jsx';
import ChatInput from '../components/chat/ChatInput.jsx';
import MessageList from '../components/chat/MessageList.jsx';
import { useSessionStore } from '../stores/sessionStore.js';
import { useChatStore } from '../stores/chatStore.js';

/**
 * 组件：ChatPage
 * 作用：聊天页容器组件，组合 ChatSidebar（会话列表）、MessageList（消息流）、ChatInput（输入区），
 * 并通过两个 store 的状态联动驱动「列表加载 / 切会话加载历史 / 滚换提示」
 * 实例化/挂载位置：路由 /chat，经 AppLayout 右侧内容区 <Outlet/> 渲染
 * 数据来源：useSessionStore（Zustand，src/stores/sessionStore.js）的 fetchList、currentSessionId；
 * useChatStore（Zustand，src/stores/chatStore.js）的 loadHistory、rolloverNotice.ts
 * 数据去向：本组件不直接发请求；fetchList 内调 POST /history/list，
 * loadHistory 内调 POST /history/detail；提示仅用 antd message，不写数据
 */
export default function ChatPage() {
  const { message } = App.useApp();
  // fetchList：sessionStore 动作，挂载时拉取会话列表（POST /history/list）
  const fetchList = useSessionStore((s) => s.fetchList);
  // currentSessionId：当前会话 ID（持久化于 sessionStorage），变化即触发下方历史加载 effect
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  // loadHistory：chatStore 动作，按会话 ID 拉取持久化消息（POST /history/detail）
  const loadHistory = useChatStore((s) => s.loadHistory);
  // rolloverTs：长对话自动滚换通知的时间戳；chatStore 在 SSE done 帧 rolled_over 时写入
  const rolloverTs = useChatStore((s) => s.rolloverNotice?.ts);

  // useEffect（依赖 []，仅挂载执行一次）：拉取会话列表，填充左侧 ChatSidebar
  useEffect(() => {
    fetchList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // useEffect（依赖 [currentSessionId]）：切换会话或刷新恢复时加载该会话历史；
  // id 为空（新对话/登出）时 chatStore 内部清空消息
  useEffect(() => {
    loadHistory(currentSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSessionId]);

  // useEffect（依赖 [rolloverTs]）：收到自动滚换通知时弹一次轻提示，告知上下文已继承
  useEffect(() => {
    if (rolloverTs) {
      message.info('对话较长，已自动接续到新会话，早期上下文已继承');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rolloverTs]);

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      <ChatSidebar />
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <MessageList />
        <ChatInput />
      </div>
    </div>
  );
}
