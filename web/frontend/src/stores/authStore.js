/**
 * @文件 stores/authStore.js
 * @作用 认证全局状态（Zustand store）：持有 JWT token 与用户身份信息（userId/userName/email/role），
 *   封装注册、账号登录、邮箱验证码登录、发送验证码、身份同步（/login/me）与登出等 action。
 *   持久化策略：token 与用户信息存 sessionStorage（key 见 TOKEN_KEY/USER_KEY）——
 *   Tab 关闭即失效以缩小 XSS 暴露面，刷新页面可从 sessionStorage 恢复登录态（初始值来源 loadPersisted）。
 *   401 由 services/http.js 全局拦截并回调 logout；role 供侧栏导航过滤（AppLayout）、
 *   路由守卫（RequireAuth/RequirePermission）与首页分流（roleHome）使用；
 *   后端鉴权以库内角色为准，历史缓存无 role 时按 'user' 兜底，不提权。
 * @主要成员 useAuthStore（状态：token/userId/userName/email/role；
 *   action：loginAccount/sendEmailCode/loginEmail/register/syncIdentity/logout；
 *   模块内部：loadPersisted/persist/applyLoginResult、TOKEN_KEY/USER_KEY/IDENTITY_SYNC_INTERVAL_MS）
 * @被谁使用 pages/LoginPage.jsx（登录/注册表单）、pages/AdminUsersPage.jsx、KnowledgePage.jsx、
 *   ProfilePage.jsx（取当前身份/防自改角色）、components/common/RequireAuth.jsx、RequirePermission.jsx、
 *   components/layout/AppLayout.jsx（导航过滤/登出入口）、router/index.jsx（角色首页分流）、
 *   main.jsx（向 http.js 绑定 tokenGetter/onUnauthorized）。
 */
import { create } from 'zustand';
import { get, post } from '../services/http.js';
import { ENDPOINTS } from '../api/contracts.js';

/** sessionStorage 持久化 key：JWT 访问令牌（同步去向：http.js 的 Authorization 头、SSE/XHR 通道）。 */
const TOKEN_KEY = 'pbl_token';
/** sessionStorage 持久化 key：用户身份信息 JSON（{userId,userName,email,role}）。 */
const USER_KEY = 'pbl_user';

/** 身份同步节流间隔（毫秒）：窗口聚焦可能高频触发，30s 内不重复请求 GET /login/me。 */
const IDENTITY_SYNC_INTERVAL_MS = 30_000;
/** 最近一次身份同步时间戳（模块级，节流判定用，不参与渲染）。 */
let _lastIdentitySyncAt = 0;

/**
 * @function loadPersisted（模块内部）
 * @description 应用初始化时从 sessionStorage 恢复 token 与用户信息（store 初始状态来源）；
 *   解析失败或无缓存时返回未登录默认值（role 兜底 'user'）。
 * @returns {{ token: string|null, userId: number|null, userName: string, email: string, role: string }}
 */
function loadPersisted() {
  try {
    const token = sessionStorage.getItem(TOKEN_KEY);
    const user = JSON.parse(sessionStorage.getItem(USER_KEY) || 'null');
    return {
      token,
      userId: user?.userId ?? null,
      userName: user?.userName || '',
      email: user?.email || '',
      role: user?.role || 'user',
    };
  } catch {
    return { token: null, userId: null, userName: '', email: '', role: 'user' };
  }
}

/**
 * @function persist（模块内部）
 * @description 同步登录态到 sessionStorage：有 token 时写入 TOKEN_KEY/USER_KEY，
 *   无 token（登出）时清除两项。
 * @param {string|null} token JWT；null 表示清除登录态。
 * @param {{userId:number, userName:string, email:string, role:string}|null} user 用户身份对象。
 * @returns {void}
 */
function persist(token, user) {
  if (token) {
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(USER_KEY, JSON.stringify(user));
  } else {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
  }
}

/**
 * Zustand 认证 store（单例 useAuthStore）。
 * 订阅方式：组件内 useAuthStore((s) => s.xxx)；命令式取值 useAuthStore.getState()。
 */
export const useAuthStore = create((set) => {
  /**
   * @function applyLoginResult（store 内部）
   * @description 登录成功统一处理（账号/邮箱两种方式共用同一响应结构）：归一化用户字段、
   *   持久化 sessionStorage、set 更新全局状态。副作用：写入 sessionStorage。
   * @param {object} data 登录接口响应体（含 access_token/user_id/user_name/email/role）。
   * @returns {object} 原始响应体（供调用方继续使用）。
   */
  function applyLoginResult(data) {
    const user = {
      userId: data.user_id ?? null,
      userName: data.user_name || data.username || String(data.user_id || ''),
      email: data.email || '',
      role: data.role || 'user',
    };
    persist(data.access_token, user);
    set({ token: data.access_token, ...user });
    return data;
  }

  return {
    // —— 状态字段 —— 初始值全部来自 loadPersisted()（即 sessionStorage 恢复值或未登录默认值）——
    /** 当前 JWT 访问令牌；null=未登录（http.js 拦截器据此决定是否注入 Authorization 头）。 */
    ...loadPersisted(),

    /**
     * @action loginAccount
     * @description 账号密码登录。
     *   副作用：POST /login/account（ENDPOINTS.LOGIN_ACCOUNT，http.js 通道）→ applyLoginResult
     *   （写 sessionStorage + 更新状态）。参数来源：LoginPage 登录表单。
     * @param {string} username 用户名/账号（表单输入）。
     * @param {string} password 密码（表单输入）。
     * @returns {Promise<object>} 登录响应体；登录成功后组件按 roleHome(role) 跳转默认首页。
     * @throws {ApiError} 凭证错误时后端以 HTTP 200+fail 防枚举文案返回，http.js 抛出后由表单提示。
     */
    async loginAccount(username, password) {
      const data = await post(ENDPOINTS.LOGIN_ACCOUNT, { username, password });
      return applyLoginResult(data);
    },

    /**
     * @action sendEmailCode
     * @description 发送邮箱登录验证码（60s 冷却由后端限流拦截）。参数来源：LoginPage 邮箱表单。
     * @param {string} email 目标邮箱（表单输入）。
     * @returns {Promise<object>} 完整响应体；页面据此开始 60s 倒计时。
     * @throws {ApiError} 发送过频/邮箱非法时抛出，页面 message.error 提示。
     */
    async sendEmailCode(email) {
      return post(ENDPOINTS.LOGIN_EMAIL_CODE, { email });
    },

    /**
     * @action loginEmail
     * @description 邮箱 + 验证码登录。副作用同 loginAccount（POST /login/email → applyLoginResult）。
     * @param {string} email 邮箱（表单输入）。
     * @param {string} code 验证码（表单输入）。
     * @returns {Promise<object>} 登录响应体；成功后按 roleHome(role) 跳转。
     * @throws {ApiError} 验证码错误/过期由 http.js 抛 ApiError，表单内提示。
     */
    async loginEmail(email, code) {
      const data = await post(ENDPOINTS.LOGIN_EMAIL, { email, code });
      return applyLoginResult(data);
    },

    /**
     * @action register
     * @description 新用户注册（不自动登录，成功后页面切回登录方式）。
     *   副作用：POST /login/register（ENDPOINTS.REGISTER）。
     * @param {string} username 用户名（注册表单，后端做密码复杂度等校验）。
     * @param {string} password 密码（至少 8 位含字母和数字）。
     * @param {string} email 邮箱。
     * @returns {Promise<object>} 完整响应体；用户名/邮箱已注册时后端返回统一防枚举文案。
     * @throws {ApiError} 校验失败/已注册时抛出，由注册表单提示。
     */
    async register(username, password, email) {
      return post(ENDPOINTS.REGISTER, { user_name: username, user_pwd: password, email });
    },

    /**
     * @action syncIdentity
     * @description 身份同步：回库 GET /login/me 拉取最新 user_name/email/role 并更新本地态与
     *   sessionStorage。管理员调整角色后用户无需重登（触发时机：页面加载/窗口聚焦，由视图层调用）。
     *   30s 节流（IDENTITY_SYNC_INTERVAL_MS）；网络抖动等非 401 错误静默忽略。
     *   状态更新去向：role 变化后订阅组件（导航/守卫）自动重渲染。
     * @returns {Promise<object|null>} 最新用户对象；节流跳过/未登录/静默失败时返回 null。
     * @throws {ApiError} 仅 401 向上抛出（交给 http.js 全局登出）。
     */
    async syncIdentity() {
      const token = useAuthStore.getState().token;
      if (!token) return null;
      const now = Date.now();
      if (now - _lastIdentitySyncAt < IDENTITY_SYNC_INTERVAL_MS) return null;
      _lastIdentitySyncAt = now;
      try {
        const data = await get(ENDPOINTS.LOGIN_ME);
        const user = {
          userId: data.user_id ?? null,
          userName: data.user_name || '',
          email: data.email || '',
          role: data.role || 'user',
        };
        const cur = useAuthStore.getState();
        if (
          cur.userId !== user.userId ||
          cur.userName !== user.userName ||
          cur.email !== user.email ||
          cur.role !== user.role
        ) {
          persist(token, user);
          set(user);
        }
        return user;
      } catch (e) {
        if (e?.httpStatus === 401) throw e; // 交给 http.js 全局登出
        return null; // 其他错误静默，不打扰用户
      }
    },

    /**
     * @action logout
     * @description 登出：清除 sessionStorage 与前端身份状态（后端无令牌撤销端点，JWT 自然过期兜底）。
     *   触发来源：AppLayout 登出按钮，以及 http.js/chatApi/fileApi 收到 401 时的全局回调。
     *   副作用：persist(null, null) 清除 TOKEN_KEY/USER_KEY；复位状态（role 回到 'user' 默认）。
     *   关联清理：会话/聊天/用量各 store 的 reset 由视图层在登出时另行调用。
     * @returns {void}
     */
    logout() {
      persist(null, null);
      set({ token: null, userId: null, userName: '', email: '', role: 'user' });
    },
  };
});
