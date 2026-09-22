/**
 * @文件 stores/usageStore.js
 * @作用 用量看板全局状态（Zustand store）：按模型聚合的 token 用量快照、按用户聚合的月度用量。
 *   仅手动刷新（页面按钮/切换月份触发），不做自动轮询——后端 /admin 路由限流 30 次/分钟。
 *   不持久化：每次进入用量页由 UsagePage 重新拉取。
 * @主要成员 useUsageStore（状态：usage/lastFetched/loading/userUsageRows/userUsageMonth/userLoading；
 *   action：fetchUsage/fetchUserUsage/reset）
 * @被谁使用 pages/UsagePage.jsx（模型维度卡片/表格、用户维度月度表格、月份选择器与手动刷新按钮）；
 *   登出时由 AppLayout 调 reset。
 */
import { create } from 'zustand';
import { fetchLlmUsage, fetchUserUsage } from '../api/usageApi.js';

/**
 * Zustand 用量看板 store（单例 useUsageStore）。
 */
export const useUsageStore = create((set) => ({
  /**
   * 按模型聚合的用量快照（初始 {}，来源：fetchUsage 即 GET /admin/llm/usage）；
   * 形态 { [model]: { requests, prompt_tokens, completion_tokens } }。
   */
  usage: {}, // { model: { requests, prompt_tokens, completion_tokens } }
  /** 最近一次成功刷新模型用量的时间（初始 null；UsagePage 可据此展示「更新于」）。 */
  lastFetched: null, // Date|null 最近成功刷新时间
  /** 模型用量加载中标志（初始 false；驱动刷新按钮/卡片加载态）。 */
  loading: false,

  /**
   * 按用户聚合的月度用量行（初始 []，来源：fetchUserUsage 即 GET /admin/llm/usage/users）；
   * 元素形态 { user_id, user_name, email, role, requests, prompt_tokens, completion_tokens }。
   */
  userUsageRows: [], // Array<{user_id, user_name, email, role, requests, prompt_tokens, completion_tokens}>
  /** 用户用量当前月份（初始 ''，成功拉取后为 YYYY-MM，与 userUsageRows 同步）。 */
  userUsageMonth: '', // YYYY-MM
  /** 用户用量加载中标志（初始 false；驱动表格/月份切换加载态）。 */
  userLoading: false,

  /**
   * @action fetchUsage
   * @description 手动刷新按模型聚合的全局用量快照（副作用：GET /admin/llm/usage）。
   *   触发来源：UsagePage 首屏加载与「刷新」按钮。
   * @returns {Promise<object>} 用量映射（同时写入 usage 与 lastFetched）。
   * @throws {ApiError} 401 跳登录/403/429 等，由调用方 catch（初始加载静默、手动刷新弹错）。
   */
  async fetchUsage() {
    set({ loading: true });
    try {
      const usage = await fetchLlmUsage();
      set({ usage, lastFetched: new Date() });
      return usage;
    } finally {
      set({ loading: false });
    }
  },

  /**
   * @action fetchUserUsage
   * @description 查询指定月份按用户聚合的用量（副作用：GET /admin/llm/usage/users?month=）。
   *   触发来源：UsagePage 首屏（当前月）、月份选择器切换、月度翻页。
   * @param {string} [month] 月份 YYYY-MM；不传由后端默认当前月。
   * @returns {Promise<{month:string, rows:Array}>} 月份与用户用量行（同时写入 userUsageRows/userUsageMonth）。
   * @throws {ApiError} 401 跳登录/403/429 等，由调用方 catch 提示。
   */
  async fetchUserUsage(month) {
    set({ userLoading: true });
    try {
      const result = await fetchUserUsage(month);
      set({ userUsageRows: result.rows, userUsageMonth: result.month });
      return result;
    } finally {
      set({ userLoading: false });
    }
  },

  /**
   * @action reset
   * @description 登出时复位全部用量看板状态为初始值。触发来源：AppLayout 登出流程。
   * @returns {void}
   */
  reset() {
    set({
      usage: {},
      lastFetched: null,
      loading: false,
      userUsageRows: [],
      userUsageMonth: '',
      userLoading: false,
    });
  },
}));
