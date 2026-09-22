/**
 * @文件 src/App.jsx
 * @作用 应用根组件：仅承载前端路由树 <AppRoutes/>（登录页公开路由、AppLayout 业务布局路由、
 *   双层登录/角色守卫、懒加载页面与角色分流均在 router/index.jsx 内声明）。
 *   本组件刻意保持无状态——Provider（antd ConfigProvider/AntdApp）、BrowserRouter、
 *   ErrorBoundary 与 http 层鉴权绑定均在 main.jsx 完成，便于测试时单独包裹路由。
 * @主要成员 App（默认导出）
 * @被谁使用 src/main.jsx 在 BrowserRouter 内渲染 <App/>，是 React 组件树的业务根。
 */
import AppRoutes from './router/index.jsx';

/**
 * @function App
 * @description 应用根组件，直接返回路由表组件。
 * @returns {JSX.Element} <AppRoutes/> 路由树。
 */
export default function App() {
  return <AppRoutes />;
}
