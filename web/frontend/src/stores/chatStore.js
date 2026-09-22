/**
 * @文件 stores/chatStore.js
 * @作用 聊天全局状态（Zustand store）：消息列表、SSE 流式接收状态机、发送/停止/中断恢复/历史加载。
 *   消息形态：{ role: 'user'|'assistant', content, streaming?: boolean, error?: string }
 *   - 流式中最后一条 assistant 消息 streaming: true；statusTip 渲染为气泡内灰字，
 *     首个 delta 到达即清空（与 docs/API.md §chat/stream 行为对齐）；
 *   - error 帧不吞内容：已收正文保留，error 字段由气泡渲染警示行；
 *   - 会话切换 = loadHistory（POST /history/detail），用自增 seq（_loadSeq）守卫防快速切换的竞态回写；
 *   - done 帧交棒 sessionStore.refreshCurrent 刷新侧栏（新会话拿到真实 id），
 *     rolled_over 时置 rolloverNotice 通知页面弹轻提示。
 *   本 store 不持久化（刷新后的消息恢复依赖 sessionStore 保存的 currentSessionId + loadHistory）。
 * @主要成员 useChatStore（状态：messages/statusTip/streaming/loadingHistory/rolloverNotice；
 *   action：loadHistory/send/recoverAfterInterrupt/abort/reset；
 *   store 内部：patchLastAssistant/finishStream）
 * @被谁使用 pages/ChatPage.jsx（发送、rolloverNotice 轻提示、会话切换联动）、
 *   components/chat/MessageList.jsx（订阅 messages/statusTip 渲染气泡与反馈按钮）、
 *   components/chat/ChatInput.jsx（订阅 streaming 驱动发送/停止按钮，调 send/abort）、
 *   components/layout/AppLayout.jsx（登出时 reset）。
 *   关联 store：useSessionStore（取 currentSessionId、done 帧回写列表）。
 */
import { create } from 'zustand';
import { recoverChat, streamChat } from '../api/chatApi.js';
import { fetchSessionDetail } from '../api/sessionApi.js';
import { createFrameDispatcher } from '../services/sse.js';
import { useSessionStore } from './sessionStore.js';

/**
 * Zustand 聊天 store（单例 useChatStore）。
 */
export const useChatStore = create((set, get) => {
  /**
   * @function patchLastAssistant（store 内部）
   * @description 局部更新最后一条 assistant 消息（流式 delta/done/error 的写入热路径）；
   *   若列表为空或末条不是 assistant 则原样返回不产生状态变更。
   * @param {Partial<{content:string, streaming:boolean, error?:string}>} patch 待合并字段。
   * @returns {void}
   */
  function patchLastAssistant(patch) {
    set((state) => {
      if (!state.messages.length) return state;
      const idx = state.messages.length - 1;
      const last = state.messages[idx];
      if (last.role !== 'assistant') return state;
      const messages = state.messages.slice();
      messages[idx] = { ...last, ...patch };
      return { messages };
    });
  }

  /**
   * @function finishStream（store 内部）
   * @description 流式结束统一收尾：清除末条 assistant 消息的 streaming 标志（保留已收内容），
   *   复位 streaming/statusTip/abortController。
   * @returns {void}
   */
  function finishStream() {
    set((state) => {
      if (!state.messages.length) return { streaming: false, statusTip: '', abortController: null };
      const idx = state.messages.length - 1;
      const last = state.messages[idx];
      const messages = state.messages.slice();
      if (last.role === 'assistant' && last.streaming) {
        messages[idx] = { ...last, streaming: false };
      }
      return { messages, streaming: false, statusTip: '', abortController: null };
    });
  }

  return {
    /**
     * 消息列表（初始 []，来源：send 乐观追加或 loadHistory 接口拉取）；
     * 订阅者：MessageList 渲染气泡。
     */
    messages: [],
    /** 流式前置阶段提示文案（status 帧 message，初始 ''）；首帧 delta 到达即清空。 */
    statusTip: '',
    /** 是否正在流式接收中（初始 false）；驱动 ChatInput 发送/停止按钮切换。 */
    streaming: false,
    /** 是否正在加载历史会话记录（初始 false）；驱动消息区加载态。 */
    loadingHistory: false,
    /**
     * 会话自动滚换通知：{ sessionId, ts } 或 null（初始 null）；
     * done 帧携带 rolled_over 且会话 id 变化时置位，由 ChatPage 监听后弹 antd 轻提示。
     */
    rolloverNotice: null,

    /**
     * @action loadHistory
     * @description 加载指定会话的持久化聊天记录（切换会话/刷新恢复时由 ChatPage effect 调用）；
     *   sessionId 为空时清空消息。用自增 _loadSeq 丢弃过期回写防止快速切换竞态。
     *   副作用：调 api/sessionApi.fetchSessionDetail（POST /history/detail）。
     * @param {number|null} sessionId 目标会话 ID（来自 sessionStore.currentSessionId）。
     * @returns {Promise<void>}
     */
    async loadHistory(sessionId) {
      if (!sessionId) {
        set({ messages: [], statusTip: '', error: undefined });
        return;
      }
      const seq = (get()._loadSeq || 0) + 1;
      set({ loadingHistory: true, _loadSeq: seq, messages: [], statusTip: '' });
      try {
        const rows = await fetchSessionDetail(sessionId);
        if (get()._loadSeq !== seq) return; // 已切走：丢弃过期回写
        set({
          messages: rows.map((m) => ({
            // 后端字段为 role（兼容历史 type 字段）
            role: (m.role || m.type) === 'user' ? 'user' : 'assistant',
            content: m.content,
          })),
        });
      } finally {
        if (get()._loadSeq === seq) set({ loadingHistory: false });
      }
    },

    /**
     * @action send
     * @description 发送一条用户消息并消费 SSE 流：乐观追加 user/空 assistant 两条消息，
     *   构造帧分发器（status→statusTip；delta→追加正文；done→回补答案并刷新 sessionStore；
     *   error→气泡错误行），通过 api/chatApi.streamChat 连接 POST /chat/stream。
     *   建连/读流异常时调 recoverAfterInterrupt 向 POST /chat/recover 核实是否已生成。
     *   参数来源：ChatInput 发送按钮/回车事件。状态更新去向：MessageList、ChatInput 订阅渲染。
     * @param {string} text 用户输入（1-4000 字符校验由输入组件/后端负责；空白或流式中直接忽略）。
     * @returns {Promise<void>}
     */
    async send(text) {
      if (get().streaming || !text.trim()) return;
      const controller = new AbortController();
      const sessionId = useSessionStore.getState().currentSessionId;

      set((state) => ({
        streaming: true,
        statusTip: '',
        abortController: controller,
        messages: [
          ...state.messages,
          { role: 'user', content: text },
          { role: 'assistant', content: '', streaming: true },
        ],
      }));

      const dispatcher = createFrameDispatcher({
        onStatus: (_stage, message) => set({ statusTip: message }),
        onDelta: (content) => {
          set({ statusTip: '' }); // 首个 delta 到达即清除状态提示
          set((state) => {
            const idx = state.messages.length - 1;
            const last = state.messages[idx];
            if (last.role !== 'assistant') return state;
            const messages = state.messages.slice();
            messages[idx] = { ...last, content: last.content + content };
            return { messages };
          });
        },
        onDone: (frame) => {
          patchLastAssistant({
            content: frame.ai_output ?? get().messages[get().messages.length - 1]?.content ?? '',
            streaming: false,
            error: undefined,
          });
          set({ streaming: false, statusTip: '', abortController: null });
          const prevSessionId = useSessionStore.getState().currentSessionId;
          useSessionStore.getState().refreshCurrent(frame.session_id, frame.title);
          // 长对话自动滚换：done 帧交回接续会话 id（currentSessionId 变化会自动
          // loadHistory 拉出迁移的最近 N 轮），同时置通知由页面弹轻提示
          if (frame.rolled_over && frame.session_id && frame.session_id !== prevSessionId) {
            set({ rolloverNotice: { sessionId: frame.session_id, ts: Date.now() } });
          }
        },
        onError: (message) => {
          patchLastAssistant({ streaming: false, error: message });
          set({ streaming: false, statusTip: '', abortController: null });
        },
      });

      try {
        await streamChat({
          userInput: text,
          sessionId,
          signal: controller.signal,
          onFrame: dispatcher,
        });
      } catch (e) {
        // 建连失败/读流中断（网络问题）：向服务端核实中断前该轮是否已生成（查短期记忆），
        // 已生成则回补答案（不重新生成、不重复调用模型）；未生成则提示重新发送。
        const fallbackMsg = e?.message || '服务处理失败';
        const handled = await get().recoverAfterInterrupt(sessionId, text, fallbackMsg);
        if (!handled) {
          patchLastAssistant({ streaming: false, error: fallbackMsg });
        }
        set({ streaming: false, statusTip: '', abortController: null });
      } finally {
        finishStream();
      }
    },

    /**
     * @action recoverAfterInterrupt
     * @description 网络中断后的幂等恢复：调 api/chatApi.recoverChat（POST /chat/recover，
     *   服务端只读短期记忆、不重新调用模型），仅当本地等待中的提问与服务端最后一轮为同一轮时回写。
     *   completed 且 user_content 一致 → 回补 ai_output；missing → 气泡追加「请重新发送」提示。
     * @param {number} sessionId 中断时的会话 ID（来自 send 内读取的 currentSessionId）。
     * @param {string} userText 中断前用户发送的文本（用于同一轮校验）。
     * @param {string} fallbackMsg 建连/读流错误文案（拼入提示）。
     * @returns {Promise<boolean>} true=已按恢复结果处理（completed/missing，或本地已进入别的轮次）；
     *   false=无法恢复（新会话无 sessionId、恢复请求本身失败），调用方保留原错误展示。
     */
    async recoverAfterInterrupt(sessionId, userText, fallbackMsg) {
      if (!sessionId) return false;
      let info;
      try {
        info = await recoverChat(sessionId);
      } catch {
        return false; // 恢复接口不可达：交由调用方展示网络错误
      }
      const msgs = get().messages;
      const sameTurn =
        msgs.length >= 2 &&
        msgs[msgs.length - 2].role === 'user' &&
        msgs[msgs.length - 2].content === userText;
      if (!sameTurn) return true; // 本地已进入别的轮次，不回写

      if (
        info.recover_status === 'completed' &&
        // 短期记忆返回的是该会话最后一轮已生成内容：须与等待中的提问是同一轮才回补，
        // 避免把更早轮次的回答误填进当前气泡
        info.user_content === userText &&
        info.ai_output
      ) {
        patchLastAssistant({ content: info.ai_output, streaming: false, error: undefined });
      } else {
        const hint = info.message || '上一条回答未完成，请重新发送';
        patchLastAssistant({
          streaming: false,
          error: fallbackMsg ? `${fallbackMsg}；${hint}` : hint,
        });
      }
      return true;
    },

    /**
     * @action abort
     * @description 用户中途停止生成：中止当前 AbortController；SSE 层静默返回、已收内容保留。
     *   触发来源：ChatInput 停止按钮。
     * @returns {void}
     */
    abort() {
      get().abortController?.abort();
    },

    /**
     * @action reset
     * @description 清空本地聊天状态（登出时由 AppLayout 调用）：先中止在途请求，
     *   再清空消息列表与流式标志。
     * @returns {void}
     */
    reset() {
      get().abortController?.abort();
      set({ messages: [], statusTip: '', streaming: false, abortController: null });
    },
  };
});
