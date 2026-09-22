/**
 * @文件 RequirePermission.jsx
 * @作用 权限路由守卫：未登录跳转登录页（防御性双保险，父级 RequireAuth 已兜底）；
 * 角色不满足时渲染 403 降级页（企业级语义：用户已登录但无权，不能误跳登录）。
 * @主要成员 RequirePermission（默认导出，组件）
 * @被谁使用 src/router/index.jsx 引入，在各业务子路由 element 中包裹懒加载页面
 * （roles 取自 nav-config.jsx 的 MAIN_NAV[].roles），是第二层（角色）守卫
 */
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Button, Result } from 'antd';
import { useAuthStore } from '../../stores/authStore.js';
import { hasRole, roleHome } from '../../services/permission.js';

/**
 * 组件：RequirePermission
 * 作用：角色权限守卫——未登录跳 /login（防御性双保险）；已登录但角色不在允许名单时渲染 403 页；
 * 满足则放行子节点
 * 实例化/挂载位置：src/router/index.jsx 各业务子路由 element（包裹 Suspense + 懒加载页面）
 * 数据来源：useAuthStore（src/stores/authStore.js）的 token、role；
 * services/permission.js 的纯函数 hasRole（含 normalizeRole 安全兜底）与 roleHome（角色首页）
 * 数据去向：不调接口；未登录渲染 <Navigate to="/login">；403 页「返回首页」按钮
 * navigate(roleHome(role))（admin→/users，user/teacher→/chat）
 * @param {object} props 组件 props
 * @param {string[]} props.roles 允许访问的角色名单（必填，来自 MAIN_NAV 项的 roles；空名单等价于无人可访问）
 * @param {import('react').ReactNode} props.children 校验通过后放行渲染的子节点（必填，实际为业务页面）
 */
export default function RequirePermission({ roles, children }) {
  // token：登录令牌（authStore）；防御性再判一次未登录（外层 RequireAuth 已兜底）
  const token = useAuthStore((s) => s.token);
  // role：当前角色（authStore，经 /login/me 同步）；未知角色由 hasRole 归一为 user，绝不提权
  const role = useAuthStore((s) => s.role);
  const location = useLocation();
  const navigate = useNavigate();

  if (!token) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  // 权限条件渲染：角色不匹配显示 403 而非跳登录（用户已认证，仅无授权）
  if (!hasRole(role, roles || [])) {
    return (
      <Result
        status="403"
        title="403"
        subTitle="抱歉，你没有权限访问此页面"
        extra={<Button type="primary" onClick={() => navigate(roleHome(role))}>返回首页</Button>}
      />
    );
  }
  return children;
}
