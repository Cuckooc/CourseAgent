/**
 * @文件 router/nav-config.jsx
 * @作用 主导航配置单一来源：侧边栏菜单（AppLayout）与路由表（router/index.jsx）共用同一份数组，
 *   新增/下线导航页只需改此文件。每项声明路径 key、中文 label、菜单图标与允许访问的角色 roles。
 *   角色判定两处生效：AppLayout 用 filterNavByRole 按角色过滤，无权限的项不渲染菜单；
 *   直接访问 URL 时由 router/index.jsx 的 RequirePermission 守卫读取同一 roles，无权则渲染 403 页面。
 * @主要成员 MAIN_NAV
 * @被谁使用 components/layout/AppLayout.jsx（侧边栏菜单渲染 + 选中态）、
 *   router/index.jsx（MAIN_NAV.map 动态生成业务子路由）。
 */
import {
  AuditOutlined,
  BarChartOutlined,
  CommentOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  IdcardOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import { ROLES } from '../services/permission.js';

// 角色别名解构：USER 普通用户、TEACHER 教师、ADMIN 管理员（语义见 services/permission.js）
const { USER, TEACHER, ADMIN } = ROLES;

/**
 * 主导航项数组（顺序即侧边栏展示顺序）。
 * @constant {Array<{key: string, label: string, icon: JSX.Element, roles: Array<'user'|'teacher'|'admin'>}>}
 *   key 同时作为路由路径（/ 开头）与 PAGE_BY_KEY 映射键；roles 为允许角色白名单。
 */
export const MAIN_NAV = [
  // 对话咨询页 /chat：user、teacher 可用；admin 无对话功能（后端 chat_router 亦 forbid_admin）
  { key: '/chat', label: '对话咨询', icon: <CommentOutlined />, roles: [USER, TEACHER] },
  // 历史信息页 /history：user、teacher 可用（长期记忆会话浏览）
  { key: '/history', label: '历史信息', icon: <HistoryOutlined />, roles: [USER, TEACHER] },
  // 知识库页 /knowledge：三种角色均可（user/teacher 管理私有库，teacher 额外管公共库，admin 仅查看）
  { key: '/knowledge', label: '知识库', icon: <DatabaseOutlined />, roles: [USER, TEACHER, ADMIN] },
  // 文档审核页 /review：三种角色均可（user/teacher 管理本人上传；teacher/admin 额外有「全部审核」代审队列）
  { key: '/review', label: '文档审核', icon: <AuditOutlined />, roles: [USER, TEACHER, ADMIN] },
  // 个人信息页 /account：user、teacher 可用（对应用户画像；页面路径刻意避开后端 /profile API）
  { key: '/account', label: '个人信息', icon: <IdcardOutlined />, roles: [USER, TEACHER] },
  // 用户管理页 /users：仅 admin（全部用户列表、角色调整、注销账户；admin 默认首页）
  { key: '/users', label: '用户管理', icon: <TeamOutlined />, roles: [ADMIN] },
  // 用量统计页 /usage：仅 admin（LLM 按模型/按用户用量看板；admin 默认首页分流到 /users）
  { key: '/usage', label: '用量统计', icon: <BarChartOutlined />, roles: [ADMIN] },
];
