/**
 * @文件 MessageList.jsx
 * @作用 消息流：user 右 / assistant 左气泡；流式中 assistant 气泡尾部渲染光标，
 * statusTip 渲染为最后一条 assistant 气泡内灰字（首个 delta 到达即消失），
 * error 渲染为气泡尾部警示行（不吞已收正文）。内容一律 {} 插值（XSS 安全）。
 * @主要成员 MessageList（默认导出，组件）；Bubble（内部单条消息气泡组件）；
 * WELCOME_EXAMPLES（空会话引导问题常量）
 * @被谁使用 src/pages/ChatPage.jsx 引入，渲染在聊天页右侧主区域（ChatInput 之上）；
 * MessageList 无 props；Bubble 仅由 MessageList 内部 map 渲染并传入消息/反馈等 props
 */
import { useEffect, useRef, useState } from 'react';
import { Button, message, Spin, Typography } from 'antd';
import {
  DislikeFilled,
  DislikeOutlined,
  LikeFilled,
  LikeOutlined,
  LoadingOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { useChatStore } from '../../stores/chatStore.js';
import { useSessionStore } from '../../stores/sessionStore.js';
import { submitFeedback } from '../../api/chatApi.js';

/** WELCOME_EXAMPLES：空会话欢迎页的示例问题，点击任一问题直接经 chatStore.send 发起对话 */
const WELCOME_EXAMPLES = ['这门课程适合什么基础的学生？', '请介绍一下 PBL 项目的学习路径', '如何获取课程资料？'];

/**
 * 组件：Bubble
 * 作用：单条消息气泡——按 role 决定左右布局与配色，渲染流式光标、状态提示、错误警示，
 * 并在已完成的 AI 回答旁展示点赞/点踩按钮
 * 实例化/挂载位置：仅由 MessageList 遍历 messages 时渲染（非导出组件）
 * 数据来源：全部来自父组件 MessageList 传入的 props（见下），无 store 直接调用
 * 数据去向：点击反馈按钮 → props.onFeedback(assistantIdx, rating)，由父组件提交到 /chat/feedback
 * @param {object} props 组件 props（均由 MessageList 传入）
 * @param {{role:'user'|'assistant', content:string, streaming?:boolean, error?:string}} props.message
 *   消息对象（必填，来自 chatStore.messages；解构时重命名为 msg 以避免遮蔽 antd 的 message）
 * @param {boolean} props.showStatusTip 是否在该气泡显示状态提示（仅最后一条消息为 true）
 * @param {string} props.statusTip 状态提示文案（chatStore.statusTip，如「检索中…」）
 * @param {number|undefined} props.assistantIdx 该 assistant 消息的轮次序号（user 消息为 undefined）
 * @param {1|-1|undefined} props.feedbackRating 本气泡已提交的反馈（控制点赞/点踩高亮）
 * @param {(assistantIdx:number, rating:1|-1)=>Promise<void>} props.onFeedback
 *   反馈回调（必填于可反馈的气泡）；点击赞/踩时触发，父组件负责 POST /chat/feedback 与错误提示
 */
function Bubble({ message: msg, showStatusTip, statusTip, assistantIdx, feedbackRating, onFeedback }) {
  const isUser = msg.role === 'user';
  // canFeedback：派生条件——仅「已完成、无错误、有序号」的 assistant 气泡显示反馈按钮
  const canFeedback = !isUser && !msg.streaming && !msg.error && assistantIdx !== undefined;
  // submitting：反馈请求提交中，禁用两个反馈按钮防重复点击；由 handleClick 切换
  const [submitting, setSubmitting] = useState(false);

  /**
   * @function handleClick（Bubble 内部）
   * @description 点赞/点踩点击处理器：防重入后调用父组件传入的 onFeedback 回调
   * 被谁触发：气泡旁点赞（rating=1）/点踩（rating=-1）按钮 onClick
   * @param {1|-1} rating 反馈值：1=点赞，-1=点踩（来自被点击按钮的固定入参）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 props.onFeedback（最终 POST /chat/feedback）；setSubmitting 包裹请求过程
   */
  async function handleClick(rating) {
    if (submitting) return;
    setSubmitting(true);
    try {
      await onFeedback(assistantIdx, rating);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start', marginBottom: 12 }}>
      <div
        style={{
          maxWidth: '78%',
          padding: '10px 14px',
          borderRadius: isUser ? '12px 12px 2px 12px' : '12px 12px 12px 2px',
          background: isUser ? '#1677ff' : '#f4f4f5',
          color: isUser ? '#fff' : 'inherit',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          lineHeight: 1.6,
        }}
      >
        {msg.content}
        {msg.streaming && <span style={{ opacity: 0.6 }}>▍</span>}
        {!isUser && showStatusTip && statusTip && (
          <div style={{ color: '#999', fontSize: 12, marginTop: msg.content ? 6 : 0 }}>
            <LoadingOutlined style={{ marginRight: 6 }} />
            {statusTip}
          </div>
        )}
        {msg.error && (
          <div style={{ color: '#cf1322', fontSize: 12, marginTop: 6 }}>
            <WarningOutlined style={{ marginRight: 6 }} />
            {msg.error}
          </div>
        )}
      </div>
      {canFeedback && (
        <div style={{ alignSelf: 'flex-end', display: 'flex', gap: 2, marginLeft: 8 }}>
          <Button
            type="text"
            size="small"
            disabled={submitting}
            loading={submitting && feedbackRating !== 1}
            icon={feedbackRating === 1 ? <LikeFilled /> : <LikeOutlined />}
            onClick={() => handleClick(1)}
            style={feedbackRating === 1 ? { color: '#1677ff' } : { color: '#999' }}
          />
          <Button
            type="text"
            size="small"
            disabled={submitting}
            loading={submitting && feedbackRating !== -1}
            icon={feedbackRating === -1 ? <DislikeFilled /> : <DislikeOutlined />}
            onClick={() => handleClick(-1)}
            style={feedbackRating === -1 ? { color: '#cf1322' } : { color: '#999' }}
          />
        </div>
      )}
    </div>
  );
}

/**
 * 组件：MessageList
 * 作用：消息流主区域——加载历史时显示 Spin、空会话显示欢迎引导、有消息时渲染气泡列表并自动滚到底；
 * 管理 AI 回答的点赞/点踩反馈状态
 * 实例化/挂载位置：ChatPage 右侧栏上部（占满剩余高度，ChatInput 之上）
 * 数据来源：useChatStore（src/stores/chatStore.js）的 messages/statusTip/loadingHistory/send；
 * useSessionStore（src/stores/sessionStore.js）的 currentSessionId；
 * 反馈接口 submitFeedback（src/api/chatApi.js，POST /chat/feedback）
 * 数据去向：欢迎语点击调 chatStore.send（POST /chat/stream SSE）；
 * 反馈提交 POST /chat/feedback（session_id/message_index/rating）；本组件不接收 props
 */
export default function MessageList() {
  // messages：当前会话消息数组（chatStore；切换会话由 loadHistory 从 /history/detail 重建）
  const messages = useChatStore((s) => s.messages);
  // statusTip：流式前置阶段提示（检索/改写等），仅传给最后一条 assistant 气泡展示
  const statusTip = useChatStore((s) => s.statusTip);
  // loadingHistory：历史记录加载中（chatStore.loadHistory 切换），整区显示 Spin
  const loadingHistory = useChatStore((s) => s.loadingHistory);
  // send：chatStore 发送动作，空会话欢迎页点击示例问题时直接调用
  const send = useChatStore((s) => s.send);
  // currentSessionId：当前会话 ID（sessionStore），反馈必带、空会话提示也据此判断
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  // bottomRef：列表底部占位元素，消息更新时 scrollIntoView 实现自动滚底
  const bottomRef = useRef(null);
  // feedbackGiven：本会话已提交反馈 { [assistantIdx]: 1|-1 }，仅前端会话内记忆（无查询接口）
  const [feedbackGiven, setFeedbackGiven] = useState({});

  /**
   * useEffect（依赖 [currentSessionId]）：切换会话时清空反馈状态，
   * 避免会话间状态串扰（后端无查询已有反馈接口，刷新后状态本就重置）
   */
  useEffect(() => {
    setFeedbackGiven({});
  }, [currentSessionId]);

  /**
   * @function handleFeedback
   * @description 提交某条 AI 回答的点赞/点踩，并在成功后记录本地反馈态用于按钮高亮
   * 被谁触发：作为 onFeedback 传入 Bubble，由气泡反馈按钮点击（经 Bubble.handleClick）调用
   * @param {number} assistantIdx assistant 轮次序号（从 0 起，按 assistant 出现顺序计数）
   * @param {1|-1} rating 1=点赞，-1=点踩
   * @returns {Promise<void>} 无返回值
   * @副作用 无会话时 message.warning 拦截；否则调 POST /chat/feedback（submitFeedback）；
   * 成功 setFeedbackGiven，失败 message.error
   */
  async function handleFeedback(assistantIdx, rating) {
    if (!currentSessionId) {
      message.warning('会话未建立，无法提交反馈');
      return;
    }
    try {
      await submitFeedback({
        sessionId: currentSessionId,
        messageIndex: assistantIdx,
        rating,
      });
      setFeedbackGiven((prev) => ({ ...prev, [assistantIdx]: rating }));
    } catch (e) {
      message.error(e?.message || '反馈提交失败');
    }
  }

  // lastIdx：最后一条消息的下标，用于判断「仅最后一条气泡」是否展示 statusTip
  const lastIdx = messages.length - 1;
  // 渲染辅助计数（每次 render 从头重建）：
  // message_index 约定：按 role=assistant 出现顺序从 0 编号（与后端 chat_feedback
  // message_index 注释「对话轮次（从0开始）」语义一致；后端每轮写 user+assistant 两条）
  let assistantCounter = 0;

  /**
   * useEffect（依赖 [messages, statusTip]）：消息内容或状态提示变化时平滑滚动到底部，
   * 保证流式 delta 追加与历史加载后视图跟随最新内容
   */
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, statusTip]);

  if (loadingHistory) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
        }}
      >
        <Spin />
        <Typography.Text type="secondary">加载会话记录…</Typography.Text>
      </div>
    );
  }

  if (!messages.length) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 16,
          padding: 24,
        }}
      >
        <Typography.Title level={4} type="secondary" style={{ marginBottom: 0 }}>
          你好，我是课程咨询助手
        </Typography.Title>
        <Typography.Text type="secondary">可以从下面的问题开始：</Typography.Text>
        {WELCOME_EXAMPLES.map((q) => (
          <Typography.Link
            key={q}
            onClick={() => send(q)}
            style={{ border: '1px solid #e5e5e5', borderRadius: 8, padding: '6px 14px' }}
          >
            {q}
          </Typography.Link>
        ))}
        {!currentSessionId && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            发送后将自动创建新会话
          </Typography.Text>
        )}
      </div>
    );
  }

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
      {messages.map((msg, idx) => {
        const isAssistant = msg.role === 'assistant';
        const assistantIdx = isAssistant ? assistantCounter : undefined;
        if (isAssistant) assistantCounter += 1;
        return (
          <Bubble
            key={idx}
            message={msg}
            showStatusTip={idx === lastIdx}
            statusTip={statusTip}
            assistantIdx={assistantIdx}
            feedbackRating={feedbackGiven[assistantIdx]}
            onFeedback={handleFeedback}
          />
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
