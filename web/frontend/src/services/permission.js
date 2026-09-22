/**
 * @文件 services/permission.js
 * @作用 权限工具集（纯函数，无 React/store 依赖，可单测）：角色枚举、角色归一化、
 *   角色命中判定、导航项按角色过滤、角色默认首页。
 *   角色来源：登录响应 / GET /login/me 返回并保存在 authStore 的 role 声明
 *   （'user' | 'teacher' | 'admin'）；后端鉴权以库内角色为准，本模块只负责前端展示与守卫。
 *   权限判定规则：
 *   - user：普通用户——对话/历史/私有知识库/文档审核/个人信息；
 *   - teacher：教师——在 user 基础上可上传/管理公共知识库；
 *   - admin：管理员——纯管理角色（公共知识库查看、用户管理与注销、用量统计），
 *     无对话/历史/私有知识库/画像功能；
 *   - 历史令牌/旧缓存缺失 role 时一律归一为普通用户，绝不因数据缺失而提权（安全默认）。
 * @主要成员 ROLES、normalizeRole、hasRole、filterNavByRole、roleHome
 * @被谁使用 router/nav-config.jsx（ROLES 定义导航可见角色）、router/index.jsx（roleHome 首页分流）、
 *   components/common/RequirePermission.jsx（hasRole/roleHome 路由守卫）、
 *   components/layout/AppLayout.jsx（filterNavByRole/normalizeRole/ROLES 侧栏过滤）、
 *   pages/LoginPage.jsx（roleHome 登录后跳转）。
 */

/**
 * 角色枚举常量：USER 普通用户 / TEACHER 教师 / ADMIN 管理员。
 * @constant {Object<string, 'user'|'teacher'|'admin'>}
 */
export const ROLES = {
  USER: 'user',
  TEACHER: 'teacher',
  ADMIN: 'admin',
};

/**
 * @function normalizeRole
 * @description 将任意输入归一为合法角色：仅 teacher/admin 原样返回，其余（undefined/null/脏值）一律按普通用户兜底。
 * @param {string} role 待归一的角色值（来自 authStore.role / sessionStorage 缓存）。
 * @returns {'user'|'teacher'|'admin'} 归一后的角色。
 */
export function normalizeRole(role) {
  return role === ROLES.ADMIN || role === ROLES.TEACHER ? role : ROLES.USER;
}

/**
 * @function hasRole
 * @description 判断给定角色是否命中允许列表（先归一化再判定，供路由守卫/按钮级条件渲染调用）。
 * @param {string} role 当前用户角色（authStore.role）。
 * @param {string[]} allowedRoles 允许访问/渲染的角色列表（来自 nav-config 的 item.roles 或组件传参）。
 * @returns {boolean} true=有权限；false=无权限（守卫渲染 403，按钮隐藏）。
 */
export function hasRole(role, allowedRoles) {
  return allowedRoles.includes(normalizeRole(role));
}

/**
 * @function filterNavByRole
 * @description 按角色过滤导航项：仅保留 item.roles 包含当前角色的项。
 * @param {Array<{key: string, label: string, icon: object, roles: string[]}>} items
 *   导航配置数组（来自 router/nav-config.jsx 的 MAIN_NAV）。
 * @param {string} role 当前用户角色（authStore.role）。
 * @returns {Array} 当前角色可见的导航项子集（供 AppLayout 渲染侧边栏菜单）。
 */
export function filterNavByRole(items, role) {
  const r = normalizeRole(role);
  return items.filter((i) => (i.roles || []).includes(r));
}

/**
 * @function roleHome
 * @description 返回角色对应的默认首页路径：admin 落用户管理 /users，teacher/user 落对话咨询 /chat。
 *   用于登录成功跳转、根路径 index 分流与任意未匹配兜底路由（*）。
 * @param {string} role 当前用户角色（authStore.role）。
 * @returns {'/users'|'/chat'} 默认首页路径。
 */
export function roleHome(role) {
  return normalizeRole(role) === ROLES.ADMIN ? '/users' : '/chat';
}
