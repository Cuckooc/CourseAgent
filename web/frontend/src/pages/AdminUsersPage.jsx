/**
 * @文件 AdminUsersPage.jsx
 * @作用 用户管理页（仅 admin）：
 * - 全部用户列表（ID / 用户名 / 邮箱 / 角色）
 * - 角色调整：user ⇄ teacher 下拉即时生效（teacher 可上传公共知识库，由管理员授予）
 * - 注销用户账户：二次确认；级联删除其会话/历史/画像，token 立即失效
 * - 防护：admin 行与当前登录账号不可调整角色/注销（后端同规则兜底）
 * @主要成员 AdminUsersPage（默认导出，页面组件）；常量 ROLE_META（角色标签/颜色）、
 * ASSIGNABLE_ROLES（下拉可选角色 user/teacher）；内部函数 refresh、handleDeactivate、handleRoleChange
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /users；
 * 仅允许角色 admin（MAIN_NAV），外层 RequireAuth + RequirePermission 守卫，其他角色直达渲染 403；
 * 渲染在 AppLayout 的 <Outlet/> 中；也是 admin 登录后的默认首页（roleHome）
 */
import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Popconfirm, Select, Table, Tag, Typography } from 'antd';
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import { deactivateAdminUser, fetchAdminUsers, updateAdminUserRole } from '../api/adminApi.js';
import { useAuthStore } from '../stores/authStore.js';

/** 角色 → Tag 颜色与中文标签映射（admin 管理员 / teacher 教师 / user 用户） */
const ROLE_META = {
  admin: { color: 'orange', label: '管理员' },
  teacher: { color: 'blue', label: '教师' },
  user: { color: 'default', label: '用户' },
};

/** 角色下拉可选项：仅允许在 user ⇄ teacher 间调整（admin 不由此授予/撤销） */
const ASSIGNABLE_ROLES = [
  { value: 'user', label: '用户' },
  { value: 'teacher', label: '教师' },
];

/**
 * 组件：AdminUsersPage
 * 作用：admin 用户管理台：全量用户表格、行内角色即时调整、二次确认注销账户
 * 实例化/挂载位置：路由 /users，经 AppLayout 的 <Outlet/> 渲染（admin 角色首页）
 * 数据来源：adminApi（src/api/adminApi.js）：fetchAdminUsers（GET /admin/users）、
 * updateAdminUserRole（PUT /admin/users/{id}/role）、deactivateAdminUser（POST .../deactivate/preview+confirm 两步注销）；
 * useAuthStore（src/stores/authStore.js）的 userId（识别当前账号行并锁定操作）
 * 数据去向：角色调整 PUT、注销两步 POST 均即时提交，成功后 refresh() 重拉全量列表
 */
export default function AdminUsersPage() {
  const { message } = App.useApp();
  // selfId：当前登录 admin 的用户 ID（authStore.userId），本行禁止改角色/注销
  const selfId = useAuthStore((s) => s.userId);

  // rows：全部用户行（接口返回后补 key=id 供 antd Table 使用）
  const [rows, setRows] = useState([]);
  // loading：列表加载/刷新中，驱动 Table 与刷新按钮 loading；由 refresh 切换
  const [loading, setLoading] = useState(false);
  // deactivating：正在注销的用户 ID，驱动对应行注销按钮 loading；null 表示无进行中注销
  const [deactivating, setDeactivating] = useState(null);
  // roleUpdating：正在调整角色的用户 ID，禁用并 loading 对应行下拉；null 表示无进行中调整
  const [roleUpdating, setRoleUpdating] = useState(null);

  /**
   * @function refresh（useCallback 记忆化）
   * @description 拉取全部用户列表并补 antd 行 key
   * 被谁触发：挂载 effect；页头「刷新」按钮；角色调整/注销成功后
   * @returns {Promise<void>} 无返回值
   * @副作用 调 GET /admin/users（fetchAdminUsers）；setRows、setLoading；失败静默留空
   */
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchAdminUsers();
      setRows((res.users || []).map((u) => ({ ...u, key: u.id })));
    } catch {
      // 初始化加载失败：静默，列表保持空白，不打扰用户
    } finally {
      setLoading(false);
    }
  }, [message]);

  // useEffect（依赖 [refresh]）：挂载时拉取一次全部用户列表
  useEffect(() => {
    refresh();
  }, [refresh]);

  /**
   * @function handleDeactivate
   * @description 注销用户账户（Popconfirm 确认后的执行体）：调注销接口并刷新列表
   * 被谁触发：表格操作列 Popconfirm 的 onConfirm（按钮在弹层内再点「注销」）；参数为当前行
   * @param {{id:number, user_name:string, role:string}} row 用户行数据（来自 rows）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 deactivateAdminUser（内部串发 POST .../deactivate/preview 取令牌、
   * POST .../deactivate/confirm 执行注销，后端级联删除数据并使 token 失效）；
   * 成功 message.success 并 refresh；失败 message.error；setDeactivating 控制按钮 loading
   */
  async function handleDeactivate(row) {
    setDeactivating(row.id);
    try {
      const res = await deactivateAdminUser(row.id);
      message.success(res.message || `已注销用户 ${row.user_name}`);
      await refresh();
    } catch (e) {
      message.error(e.message || '注销失败');
    } finally {
      setDeactivating(null);
    }
  }

  /**
   * @function handleRoleChange
   * @description 行内角色下拉变更处理器：值未变化直接忽略，否则即时提交角色调整并刷新
   * 被谁触发：角色列 Select 的 onChange（仅非 admin、非自身行渲染为下拉）
   * @param {{id:number, user_name:string, role:string}} row 用户行数据（来自 rows）
   * @param {'user'|'teacher'} role 新角色，来自 ASSIGNABLE_ROLES
   * @returns {Promise<void>} 无返回值
   * @副作用 调 PUT /admin/users/{id}/role（updateAdminUserRole）；
   * 成功 message.success 并 refresh；失败 message.error；setRoleUpdating 控制下拉 loading/禁用
   */
  async function handleRoleChange(row, role) {
    if (role === row.role) return;
    setRoleUpdating(row.id);
    try {
      const res = await updateAdminUserRole(row.id, role);
      message.success(res.message || `已将用户 ${row.user_name} 的角色调整为 ${ROLE_META[role]?.label || role}`);
      await refresh();
    } catch (e) {
      message.error(e.message || '角色调整失败');
    } finally {
      setRoleUpdating(null);
    }
  }

  /**
   * columns：用户表格列配置（渲染辅助，含权限条件渲染）：
   * ID / 用户名 / 邮箱 / 角色（admin 行与当前账号只显示 Tag，其余显示可切换 Select）/
   * 操作（锁定行显示「当前账号/不可注销」，否则显示带 Popconfirm 的注销按钮）
   */
  const columns = [
    { title: 'ID', dataIndex: 'id', width: 90 },
    { title: '用户名', dataIndex: 'user_name' },
    { title: '邮箱', dataIndex: 'email' },
    {
      title: '角色',
      dataIndex: 'role',
      width: 150,
      render: (role, row) => {
        if (role === 'admin' || row.id === selfId) {
          const meta = ROLE_META[role] || ROLE_META.user;
          return <Tag color={meta.color}>{meta.label}</Tag>;
        }
        return (
          <Select
            size="small"
            style={{ width: 96 }}
            value={role}
            options={ASSIGNABLE_ROLES}
            loading={roleUpdating === row.id}
            disabled={roleUpdating === row.id}
            onChange={(value) => handleRoleChange(row, value)}
          />
        );
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, row) => {
        const locked = row.role === 'admin' || row.id === selfId;
        return locked ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.id === selfId ? '当前账号' : '不可注销'}
          </Typography.Text>
        ) : (
          <Popconfirm
            title={`确认注销用户「${row.user_name}」？`}
            description="将同时删除其全部会话、历史与画像数据，且不可恢复"
            okText="注销"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => handleDeactivate(row)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} loading={deactivating === row.id}>
              注销
            </Button>
          </Popconfirm>
        );
      },
    },
  ];

  return (
    <div style={{ padding: 16, maxWidth: 1100, margin: '0 auto' }}>
      <Card
        title="用户管理"
        extra={
          <Button icon={<ReloadOutlined />} onClick={refresh} loading={loading}>
            刷新
          </Button>
        }
        styles={{ body: { paddingTop: 12 } }}
      >
        <Table
          size="middle"
          loading={loading}
          dataSource={rows}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '暂无用户' }}
        />
      </Card>
    </div>
  );
}
