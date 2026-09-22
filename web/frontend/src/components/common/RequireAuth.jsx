/**
 * @文件 RequireAuth.jsx
 * @作用 登录守卫：未登录重定向 /login（携带回跳参数）。
 * 业务路由全部经此包裹（见 router/index.jsx）。
 * @主要成员 RequireAuth（默认导出，组件）
 * @被谁使用 src/router/index.jsx 引入，包裹根布局路由 element
 * （<RequireAuth><AppLayout/></RequireAuth>），是所有业务页面的第一层守卫
 */
import { Navigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../../stores/authStore.js';

/**
 * 组件：RequireAuth
 * 作用：登录态路由守卫——无 token 时声明式重定向到 /login 并记录来源路径，有 token 时放行子节点
 * 实例化/挂载位置：src/router/index.jsx 根布局路由（path="/"）的 element，包裹 AppLayout
 * 数据来源：useAuthStore（Zustand，src/stores/authStore.js）的 token
 * （启动时从 sessionStorage 的 pbl_token 恢复）；useLocation 的当前 pathname
 * 数据去向：不调接口；未登录仅渲染 <Navigate to="/login" state={{from}}>，
 * 登录成功后由 LoginPage 读取 state.from 回跳
 * @param {object} props 组件 props
 * @param {import('react').ReactNode} props.children 已登录时放行渲染的子节点（必填，router 中为 AppLayout）
 */
export default function RequireAuth({ children }) {
  // token：登录令牌（authStore，sessionStorage 持久化）；falsy 即视为未登录
  const token = useAuthStore((s) => s.token);
  // location：当前路由位置，pathname 作为回跳地址带给 /login
  const location = useLocation();
  if (!token) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  return children;
}
