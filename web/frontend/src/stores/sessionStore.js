/**
 * @文件 stores/sessionStore.js
 * @作用 会话列表全局状态（Zustand store）：会话列表、当前会话 ID，以及
 *   列表拉取/创建/切换/重命名/删除/SSE done 帧回写等 action。
 *   持久化配置：仅当前会话 ID 持久化到 sessionStorage（key=pbl_last_session，见 LAST_SESSION_KEY）——
 *   刷新页面后可恢复上次会话；同步去向：ChatPage 监听 currentSessionId 变化并调
 *   chatStore.loadHistory 拉取聊天记录本体（本 store 不存消息内容）。
 *   列表本身不持久化（每次进入由 fetchList 从 POST /history/list 拉取）。
 *   登出时由视图调用 reset() 清空。
 * @主要成员 useSessionStore（状态：sessions/currentSessionId/loading；
 *   action：fetchList/create/select/rename/remove/refreshCurrent/reset；
 *   模块内部：loadLastSession/persistLastSession、LAST_SESSION_KEY）
 * @被谁使用 components/chat/ChatSidebar.jsx（列表渲染/切换/新建/重命名/删除）、
 *   components/chat/ChatInput.jsx（读 currentSessionId 上传 temp 附件）、
 *   components/chat/MessageList.jsx（反馈时取会话 ID）、pages/ChatPage.jsx（首屏拉列表 +
 *   订阅 currentSessionId 联动 loadHistory）、pages/HistoryPage.jsx（删除后刷新）、
 *   components/layout/AppLayout.jsx（登出 reset）；stores/chatStore.js（send 读取与 done 回写）。
 */
import { create } from 'zustand';
import {
  createSession,
  deleteSession,
  fetchSessionList,
  undoLastDelete,
  updateSessionTitle,
} from '../api/sessionApi.js';

/** sessionStorage 持久化 key：最近访问的会话 ID（字符串形式存储，刷新恢复用）。 */
const LAST_SESSION_KEY = 'pbl_last_session';

/**
 * @function loadLastSession（模块内部）
 * @description 启动时从 sessionStorage 读取上次会话 ID（store 初始 currentSessionId 来源）；
 *   无值/非法/存储不可用时返回 null。
 * @returns {number|null} 合法正整数会话 ID，或 null。
 */
function loadLastSession() {
  try {
    const v = parseInt(sessionStorage.getItem(LAST_SESSION_KEY), 10);
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

/**
 * @function persistLastSession（模块内部）
 * @description 同步当前会话 ID 到 sessionStorage；传空值时移除该 key。
 *   存储不可用（隐私模式等）时静默失败：仅影响刷新恢复，不阻断业务。
 * @param {number|null} sessionId 当前会话 ID；null/0 表示清除指向。
 * @returns {void}
 */
function persistLastSession(sessionId) {
  try {
    if (sessionId) sessionStorage.setItem(LAST_SESSION_KEY, String(sessionId));
    else sessionStorage.removeItem(LAST_SESSION_KEY);
  } catch {
    // 存储不可用（隐私模式等）：仅影响刷新恢复，不阻断
  }
}

/**
 * Zustand 会话 store（单例 useSessionStore）。
 */
export const useSessionStore = create((set, get) => ({
  /**
   * 会话简要列表（初始 []，来源：fetchSessionList 接口 / create、refreshCurrent 的本地并入）；
   * 元素形态 { session_id:number, title:string }；订阅者：ChatSidebar。
   */
  sessions: [],
  /** 当前会话 ID（初始值来自 loadLastSession() 的 sessionStorage 恢复；null=未选择/新会话）。 */
  currentSessionId: loadLastSession(),
  /** 列表是否加载中（初始 false）；驱动侧栏列表加载态。 */
  loading: false,

  /**
   * @action fetchList
   * @description 拉取会话列表（副作用：POST /history/list）；若持久化的当前会话已不在列表中
   *   （被删除等），则清除 currentSessionId 与 sessionStorage 指向。
   *   触发来源：ChatPage 首屏与各写操作后刷新。
   * @returns {Promise<void>}
   */
  async fetchList() {
    set({ loading: true });
    try {
      const sessions = await fetchSessionList();
      const cur = get().currentSessionId;
      const stillExists = cur != null && sessions.some((s) => s.session_id === cur);
      if (cur != null && !stillExists) {
        persistLastSession(null);
        set({ sessions, currentSessionId: null });
      } else {
        set({ sessions });
      }
    } finally {
      set({ loading: false });
    }
  },

  /**
   * @action create
   * @description 创建会话并置顶置为当前（副作用：POST /history/create，成功后持久化 sessionStorage）。
   *   参数来源：ChatSidebar 新建按钮/命名弹窗。
   * @param {string} [title] 初始标题（可空，后端默认「新会话」）。
   * @returns {Promise<{session_id:number, title:string}>} 新会话对象（供调用方收尾）。
   * @throws {ApiError} 失败时向上抛出，由调用方提示且本地不变。
   */
  async create(title) {
    const created = await createSession(title);
    set((state) => ({
      sessions: [{ session_id: created.session_id, title: created.title }, ...state.sessions],
      currentSessionId: created.session_id,
    }));
    persistLastSession(created.session_id);
    return created;
  },

  /**
   * @action select
   * @description 切换当前会话：更新状态并持久化；聊天记录由 ChatPage 响应 currentSessionId
   *   变化自动调 chatStore.loadHistory 加载（本 action 不直接拉消息）。
   *   参数来源：ChatSidebar 列表项点击。
   * @param {number} sessionId 目标会话 ID。
   * @returns {void}
   */
  select(sessionId) {
    set({ currentSessionId: sessionId });
    persistLastSession(sessionId);
  },

  /**
   * @action rename
   * @description 重命名会话：先更新后端（副作用：POST /history/update_title），成功后同步本地列表标题。
   *   参数来源：ChatSidebar 内联重命名输入。
   * @param {number} sessionId 目标会话 ID。
   * @param {string} title 新标题。
   * @returns {Promise<void>}
   * @throws {ApiError} 越权/失败时抛出，本地标题不变，由调用方提示。
   */
  async rename(sessionId, title) {
    await updateSessionTitle(sessionId, title);
    set((state) => ({
      sessions: state.sessions.map((s) => (s.session_id === sessionId ? { ...s, title } : s)),
    }));
  },

  /**
   * @action remove
   * @description 删除会话（副作用：POST /history/delete/preview + confirm 两步；后端同时清理该会话
   *   的临时知识库）；本地从列表移除，若删除的正是当前会话则清空当前指向与持久化。
   *   参数来源：ChatSidebar/HistoryPage 删除操作（经 Popconfirm）。
   * @param {number} sessionId 待删除会话 ID。
   * @returns {Promise<void>}
   * @throws {ApiError} 删除失败时本地不变，由调用方提示。
   */
  async remove(sessionId) {
    await deleteSession(sessionId);
    set((state) => {
      const sessions = state.sessions.filter((s) => s.session_id !== sessionId);
      const cur = state.currentSessionId === sessionId ? null : state.currentSessionId;
      if (cur == null) persistLastSession(null);
      return { sessions, currentSessionId: cur };
    });
  },

  /**
   * @action undoRemove
   * @description 撤销最近一次删除（副作用：POST /history/undo_delete，后端一次性快照）；
   *   成功后重新 fetchList 拉回恢复的会话。注意恢复不自动切换当前会话
   *   （后端不返回恢复的 session_id），由用户在列表中自行点击。
   *   触发来源：ChatSidebar 删除成功提示条上的「撤销」按钮。
   * @returns {Promise<'session'|'profile'>} 实际恢复的对象类型，供调用方写提示文案。
   * @throws {ApiError} 404 没有可撤销记录/500 恢复失败，由调用方提示且不刷新本地状态。
   */
  async undoRemove() {
    const res = await undoLastDelete();
    await get().fetchList();
    return res.type;
  },

  /**
   * @action refreshCurrent
   * @description SSE done 帧回调（由 chatStore.send 调用）：把（可能是首条消息新建的）会话
   *   置顶并入列表或更新标题，同时设为当前会话并持久化。无 sessionId 时直接忽略。
   * @param {number} sessionId done 帧返回的真实会话 ID。
   * @param {string} [title] done 帧返回的会话标题（首条消息后由后端生成）。
   * @returns {void}
   */
  refreshCurrent(sessionId, title) {
    if (!sessionId) return;
    set((state) => {
      const exists = state.sessions.some((s) => s.session_id === sessionId);
      const sessions = exists
        ? state.sessions.map((s) =>
            s.session_id === sessionId && title ? { ...s, title } : s
          )
        : [{ session_id: sessionId, title: title || '新会话' }, ...state.sessions];
      return { sessions, currentSessionId: sessionId };
    });
    persistLastSession(sessionId);
  },

  /**
   * @action reset
   * @description 登出清理：移除持久化的会话 ID，清空列表/当前指向/加载态。
   *   触发来源：AppLayout 登出流程。
   * @returns {void}
   */
  reset() {
    persistLastSession(null);
    set({ sessions: [], currentSessionId: null, loading: false });
  },
}));
