/**
 * @文件 AppLayout.jsx
 * @作用 业务布局（参考千问式聊天布局）：
 * 左侧固定侧边栏 = 品牌 + 新对话 + 垂直导航（按角色过滤）+ 底部用户区；
 * 右侧内容区 = Outlet，占满剩余宽度、独立滚动。
 * 导航项来自 nav-config 单一来源；adminOnly 项仅 admin 角色可见
 * （URL 直达由 RequirePermission 守卫渲染 403）。
 * @主要成员 AppLayout（默认导出，组件）；内部函数 handleNewSession
 * @被谁使用 src/router/index.jsx 引入，作为根布局路由（path="/"）的 element
 * （外层 RequireAuth）；全部业务页面经其 <Outlet/> 渲染
 */
import { useEffect } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { App as AntdApp, Avatar, Button, Menu, Popconfirm, Tooltip } from 'antd';
import { LogoutOutlined, PlusOutlined, UserOutlined } from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore.js';
import { useSessionStore } from '../../stores/sessionStore.js';
import { useChatStore } from '../../stores/chatStore.js';
import { MAIN_NAV } from '../../router/nav-config.jsx';
import { filterNavByRole, normalizeRole, ROLES } from '../../services/permission.js';

/**
 * 组件：AppLayout
 * 作用：登录后业务总布局——左侧品牌/新对话/角色导航/用户区，右侧 <Outlet/> 渲染当前路由页面
 * 实例化/挂载位置：src/router/index.jsx 根布局路由（path="/"）的 element，被 RequireAuth 包裹
 * 数据来源：useAuthStore（src/stores/authStore.js）的 userName/role/logout/syncIdentity
 * （syncIdentity 内调 GET /login/me）；MAIN_NAV（src/router/nav-config.jsx）导航配置；
 * services/permission.js 的 filterNavByRole/normalizeRole/ROLES
 * 数据去向：新对话 → sessionStore.create（POST /history/create）+ chatStore.reset 后 navigate('/chat')；
 * 退出登录 Popconfirm → 重置 chat/session store + authStore.logout 后 navigate('/login')；
 * 菜单/头像点击 → navigate 到对应路由
 */
export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = AntdApp.useApp();
  // userName：当前用户名（authStore），侧栏底部展示
  const userName = useAuthStore((s) => s.userName);
  // role：当前角色（authStore），决定导航过滤结果、是否显示「新对话」与个人信息入口
  const role = useAuthStore((s) => s.role);
  // logout：authStore 登出动作（清 token/用户信息与 sessionStorage，后端无撤销端点）
  const logout = useAuthStore((s) => s.logout);
  // syncIdentity：authStore 身份同步动作（GET /login/me，store 内含 30s 节流）
  const syncIdentity = useAuthStore((s) => s.syncIdentity);

  /**
   * useEffect（依赖 [syncIdentity]）：身份同步——
   * 挂载时回库拉最新角色（管理员调整后无需重登即更新导航）；
   * 并订阅 document visibilitychange，窗口重新可见时再同步一次；
   * 清理函数移除监听。实际请求节流（30s）与静默处理在 authStore.syncIdentity 内
   */
  useEffect(() => {
    syncIdentity?.();
    const onVisible = () => {
      if (document.visibilityState === 'visible') syncIdentity?.();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [syncIdentity]);

  // isAdmin：是否纯管理角色（normalizeRole 安全归一）；admin 无对话功能，不展示「新对话」
  const isAdmin = normalizeRole(role) === ROLES.ADMIN;
  // navItems：派生值——按角色过滤后的导航项（filterNavByRole 纯函数，输入 MAIN_NAV 与 role）
  const navItems = filterNavByRole(MAIN_NAV, role);
  // selectedKey：派生值——以当前路径前缀匹配的导航项高亮，匹配不到取第一项，兜底 /knowledge
  const selectedKey =
    navItems.find((i) => location.pathname.startsWith(i.key))?.key || navItems[0]?.key || '/knowledge';

  /**
   * @function handleNewSession
   * @description 「新对话」按钮处理器：创建会话、清空聊天本地状态并跳转对话页
   * 被谁触发：侧栏「新对话」按钮 onClick（admin 角色不渲染该按钮）
   * @returns {Promise<void>} 无返回值；无入参
   * @副作用 调 sessionStore.create（POST /history/create，写 currentSessionId 并持久化）；
   * chatStore.reset 清空消息；navigate('/chat')；失败（如限流）message.error
   */
  async function handleNewSession() {
    try {
      await useSessionStore.getState().create();
      useChatStore.getState().reset();
      navigate('/chat');
    } catch (e) {
      message.error(e.message || '创建会话失败');
    }
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* 左侧边栏 */}
      <aside
        style={{
          width: 240,
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          background: '#f7f8fa',
          borderRight: '1px solid #ececec',
        }}
      >
        <div style={{ padding: '20px 20px 12px', fontSize: 17, fontWeight: 600, letterSpacing: 0.5 }}>
          智能课程咨询服务
        </div>

        {!isAdmin && (
          <div style={{ padding: '0 16px' }}>
            <Button block icon={<PlusOutlined />} onClick={handleNewSession}>
              新对话
            </Button>
          </div>
        )}

        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          items={navItems.map(({ key, label, icon }) => ({ key, label, icon }))}
          onClick={({ key }) => navigate(key)}
          style={{ borderInlineEnd: 'none', background: 'transparent', marginTop: 12, flexShrink: 0 }}
        />

        <div style={{ flex: 1 }} />

        {/* 底部用户区（admin 无个人信息页，仅展示账号名） */}
        <div
          style={{
            borderTop: '1px solid #ececec',
            padding: '12px 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          {isAdmin ? (
            <Avatar size="small" icon={<UserOutlined />} style={{ flexShrink: 0 }} />
          ) : (
            <Tooltip title="个人信息 / 用户画像">
              <Avatar
                size="small"
                icon={<UserOutlined />}
                style={{ cursor: 'pointer' }}
                onClick={() => navigate('/account')}
              />
            </Tooltip>
          )}
          <span
            role={isAdmin ? undefined : 'button'}
            tabIndex={isAdmin ? undefined : 0}
            onClick={isAdmin ? undefined : () => navigate('/account')}
            onKeyDown={(e) => !isAdmin && e.key === 'Enter' && navigate('/account')}
            style={{
              flex: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              cursor: isAdmin ? 'default' : 'pointer',
            }}
            title={userName || '用户'}
          >
            {userName || '用户'}
          </span>
          <Popconfirm
            title="确定退出登录？"
            onConfirm={() => {
              useChatStore.getState().reset();
              useSessionStore.getState().reset();
              logout();
              navigate('/login', { replace: true });
            }}
          >
            <Tooltip title="退出登录">
              <Button type="text" size="small" icon={<LogoutOutlined />} aria-label="退出登录" />
            </Tooltip>
          </Popconfirm>
        </div>
      </aside>

      {/* 右侧内容区：独立滚动，聊天页将占满整块 */}
      <main style={{ flex: 1, minWidth: 0, overflow: 'auto', background: '#fff' }}>
        <Outlet />
      </main>
    </div>
  );
}
