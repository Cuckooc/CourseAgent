/**
 * @文件 router/index.jsx
 * @作用 应用前端路由表（react-router-dom v6 声明式路由）：
 *   - /login 公开，无需登录；
 *   - 业务页统一挂在 / 下的 AppLayout 布局内，经双层守卫——
 *     RequireAuth（登录态校验：无 token 重定向 /login 并带 redirect 回跳参数）+
 *     RequirePermission（角色校验：角色不在允许列表时渲染 403，不跳转）；
 *   - 路由与侧边栏菜单共用 nav-config.jsx 的 MAIN_NAV 单一来源，新增导航只改一处；
 *   - 所有业务页面使用 React.lazy 按需加载（Suspense 兜底 Spin），减小初始包体积；
 *   - 首页 index 与兜底 * 均按角色分流：admin → /users，teacher/user → /chat（roleHome）。
 * @主要成员 AppRoutes（默认导出）、PAGE_BY_KEY（路径→懒加载页面元素映射）、
 *   LazyFallback、RoleRedirect
 * @被谁使用 src/App.jsx 直接渲染 <AppRoutes/>，并由 main.jsx 包在 BrowserRouter 内挂载。
 *
 * 路由明细（path → pages 组件 → 允许角色，守卫规则一致：先 RequireAuth 登录、再 RequirePermission 角色）：
 *   /login          → LoginPage          公开（已登录访问仍渲染登录页，登录动作内自行跳首页）
 *   /  (index)      → RoleRedirect       需登录；按角色重定向 /users 或 /chat
 *   /chat           → ChatPage           user、teacher（admin 禁止对话）
 *   /history        → HistoryPage        user、teacher
 *   /knowledge      → KnowledgePage      user、teacher、admin
 *   /review         → ReviewPage         user、teacher
 *   /account        → ProfilePage        user、teacher
 *                     注意：页面路由用 /account 而非 /profile，避免整页刷新/深链直连
 *                     命中后端画像 API GET /profile 返回 401 JSON 白屏
 *   /users          → AdminUsersPage     仅 admin
 *   /usage          → UsagePage          仅 admin
 *   *（任意未匹配）→ RoleRedirect        需登录；按角色重定向到默认首页
 */
import { Suspense, lazy } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Spin } from 'antd';
import RequireAuth from '../components/common/RequireAuth.jsx';
import RequirePermission from '../components/common/RequirePermission.jsx';
import AppLayout from '../components/layout/AppLayout.jsx';
import LoginPage from '../pages/LoginPage.jsx';
import { MAIN_NAV } from './nav-config.jsx';
import { useAuthStore } from '../stores/authStore.js';
import { roleHome } from '../services/permission.js';

// 所有业务页均按需加载，降低首屏体积
const ChatPage = lazy(() => import('../pages/ChatPage.jsx'));
const HistoryPage = lazy(() => import('../pages/HistoryPage.jsx'));
const KnowledgePage = lazy(() => import('../pages/KnowledgePage.jsx'));
const ProfilePage = lazy(() => import('../pages/ProfilePage.jsx'));
const UsagePage = lazy(() => import('../pages/UsagePage.jsx'));
const AdminUsersPage = lazy(() => import('../pages/AdminUsersPage.jsx'));
const ReviewPage = lazy(() => import('../pages/ReviewPage.jsx'));

/**
 * 路径 key（与 MAIN_NAV 的 key 一致）到懒加载页面元素的映射表；
 * 路由表由 MAIN_NAV.map 动态生成时凭此取对应组件。
 * @constant {Object<string, JSX.Element>}
 */
const PAGE_BY_KEY = {
  '/chat': <ChatPage />,
  '/history': <HistoryPage />,
  '/knowledge': <KnowledgePage />,
  '/review': <ReviewPage />,
  // 注意：页面路由用 /account，避免与后端画像 API GET /profile 同路径冲突
  // （整页刷新/深链时浏览器直连 /profile 会命中 API 返回 401 JSON 白屏）
  '/account': <ProfilePage />,
  '/users': <AdminUsersPage />,
  '/usage': <UsagePage />,
};

/**
 * @function LazyFallback
 * @description 业务页 React.lazy 懒加载期间的统一加载态（居中 antd Spin）。
 * @returns {JSX.Element} 加载占位元素。
 */
function LazyFallback() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <Spin />
    </div>
  );
}

/**
 * @function RoleRedirect
 * @description 按当前登录角色渲染命令式重定向（在组件内订阅 authStore.role，
 *   保证登录态/角色变化后随之重渲染）：admin → /users，其余 → /chat。
 *   用于 / 的 index 与 * 兜底路由。
 * @returns {JSX.Element} react-router 的 <Navigate replace> 元素。
 */
function RoleRedirect() {
  const role = useAuthStore((s) => s.role);
  return <Navigate to={roleHome(role)} replace />;
}

/**
 * @function AppRoutes
 * @description 应用全部路由声明（默认导出）。结构：
 *   /login 公开路由；/ 下 AppLayout 受 RequireAuth 保护，index 角色分流，
 *   MAIN_NAV 动态生成的业务子路由各自再包 RequirePermission + Suspense；* 兜底角色分流。
 * @returns {JSX.Element} <Routes> 路由树。
 */
export default function AppRoutes() {
  return (
    <Routes>
      {/* 公开路由：登录页，无需登录态 */}
      <Route path="/login" element={<LoginPage />} />
      {/* 业务布局路由：RequireAuth 登录守卫，未登录跳 /login（带 redirect） */}
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        {/* 首页：登录后按角色分流到默认首页 */}
        <Route index element={<RoleRedirect />} />
        {/* 业务页：角色来自 MAIN_NAV，RequirePermission 校验（无权渲染 403），页面均懒加载 */}
        {MAIN_NAV.map((item) => (
          <Route
            key={item.key}
            path={item.key.slice(1)}
            element={
              <RequirePermission roles={item.roles}>
                <Suspense fallback={<LazyFallback />}>{PAGE_BY_KEY[item.key]}</Suspense>
              </RequirePermission>
            }
          />
        ))}
      </Route>
      {/* 兜底：任意未匹配路径按角色重定向默认首页 */}
      <Route path="*" element={<RoleRedirect />} />
    </Routes>
  );
}
