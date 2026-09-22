/**
 * @文件 LoginPage.jsx
 * @作用 登录注册页：账号登录 / 邮箱验证码登录 / 注册三 Tab。
 * - 登录失败（HTTP 200 + fail）由 http.js 抛 ApiError，message 直接展示（后端防枚举文案）
 * - 验证码 60s 冷却前端倒计时（与后端冷却对齐，提前禁用按钮）
 * - 登录成功跳转 redirect 参数或角色首页（admin → /users，user/teacher → /chat）
 * @主要成员 LoginPage（默认导出，页面组件）；内部表单组件 AccountLoginForm、EmailLoginForm、RegisterForm；
 * RULES（antd Form 校验规则常量：用户名/登录密码/注册密码/邮箱/验证码）
 * @被谁使用 src/router/index.jsx 静态 import，挂载于公开路由 /login（不经登录守卫）；
 * 已登录用户访问时由组件内 <Navigate> 声明式重定向到角色首页
 */
import { useEffect, useRef, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Alert, App as AntdApp, Button, Card, Form, Input, Space, Tabs, Typography } from 'antd';
import { useAuthStore } from '../stores/authStore.js';
import { roleHome } from '../services/permission.js';

/**
 * 三个表单共用的 antd 校验规则集合：
 * 登录密码仅做 6-64 位格式约束（兼容历史弱口令账号）；注册密码与后端复杂度策略对齐
 */
const RULES = {
  username: [{ required: true, message: '请输入用户名' }, { max: 20, message: '用户名最长 20 字符' }],
  // 登录表单使用（历史账号可能仍是弱口令，仅做格式约束）
  password: [{ required: true, message: '请输入密码' }, { min: 6, max: 64, message: '密码长度 6-64 字符' }],
  // 注册表单使用（与后端密码复杂度策略一致：≥8 位且含字母和数字）
  registerPassword: [
    { required: true, message: '请输入密码' },
    { min: 8, max: 64, message: '密码长度 8-64 字符' },
    {
      pattern: /^(?=.*[A-Za-z])(?=.*\d).+$/,
      message: '密码需同时包含字母和数字',
    },
  ],
  email: [
    { required: true, message: '请输入邮箱' },
    { type: 'email', message: '邮箱格式不正确' },
  ],
  code: [{ required: true, message: '请输入验证码' }],
};

/**
 * 组件：AccountLoginForm
 * 作用：账号 + 密码登录表单，提交成功后写入登录态并按角色跳转
 * 实例化/挂载位置：作为 LoginPage「账号登录」Tab 的 children 渲染
 * 数据来源：useAuthStore（Zustand，定义于 src/stores/authStore.js）的 loginAccount 动作；
 * useLocation 的 state.from（RequireAuth 重定向前记录的来源路径，可选）
 * 数据去向：登录提交到 POST /login/account（经 authStore.loginAccount → http.js）；
 * 成功后 token/用户信息写入 authStore 与 sessionStorage，并 navigate 到角色首页或来源页
 */
function AccountLoginForm() {
  const navigate = useNavigate();
  const location = useLocation();
  // loginAccount：authStore 登录动作（POST /login/account），成功后持久化 token 与角色
  const loginAccount = useAuthStore((s) => s.loginAccount);
  // loading：登录请求进行中标志，仅由 onFinish 切换，驱动提交按钮 loading
  const [loading, setLoading] = useState(false);
  // error：登录失败文案（ApiError.message），由 onFinish 捕获后写入，渲染为表单顶部 Alert
  const [error, setError] = useState('');

  /**
   * @function onFinish
   * @description 账号登录表单提交处理器，由登录按钮点击 / 表单回车触发（antd Form onFinish）
   * @param {{username:string, password:string}} values 表单值，来自用户输入，均必填（rules 已校验）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 loginAccount 登录；成功后按角色 navigate（admin 固定 /users，其余回 from 或 /chat）；
   * 失败 setError 展示后端防枚举文案；无论成败最终 setLoading(false)
   */
  const onFinish = async (values) => {
    setLoading(true);
    setError('');
    try {
      await loginAccount(values.username, values.password);
      // admin 固定落管理首页：from 可能是守卫重定向前想访问的 /chat（admin 禁聊），
      // 回跳只会得到 403；user/teacher 保留"回到来源页"行为
      const home = roleHome(useAuthStore.getState().role);
      navigate(home === '/chat' ? location.state?.from || home : home, { replace: true });
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
      {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
      <Form.Item name="username" label="用户名" rules={RULES.username}>
        <Input placeholder="用户名" autoComplete="username" />
      </Form.Item>
      <Form.Item name="password" label="密码" rules={RULES.password}>
        <Input.Password placeholder="密码" autoComplete="current-password" />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={loading}>
        登录
      </Button>
    </Form>
  );
}

/**
 * 组件：EmailLoginForm
 * 作用：邮箱 + 验证码登录表单；负责发送验证码（含 60s 前端冷却倒计时）与验证码登录
 * 实例化/挂载位置：作为 LoginPage「邮箱登录」Tab 的 children 渲染
 * 数据来源：useAuthStore（src/stores/authStore.js）的 sendEmailCode、loginEmail 动作；
 * antd Form 实例 form（读取邮箱字段）
 * 数据去向：发送验证码 POST /login/email/code；登录 POST /login/email；
 * 成功后写 authStore 登录态并 navigate 角色首页或来源页
 */
function EmailLoginForm() {
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = AntdApp.useApp();
  // sendEmailCode：请求后端向邮箱发送验证码；loginEmail：验证码登录
  const loginEmail = useAuthStore((s) => s.loginEmail);
  const sendEmailCode = useAuthStore((s) => s.sendEmailCode);
  // form：antd Form 实例，发送验证码前仅校验/读取 email 字段
  const [form] = Form.useForm();
  // loading：验证码登录提交中，驱动登录按钮 loading；由 onFinish 切换
  const [loading, setLoading] = useState(false);
  // sending：发送验证码请求中，驱动「发送验证码」按钮 loading；由 handleSendCode 切换
  const [sending, setSending] = useState(false);
  // cooldown：发送后剩余冷却秒数（60→0），>0 时按钮禁用并显示倒计时；由 startCooldown 每秒递减
  const [cooldown, setCooldown] = useState(0);
  // error：登录/发送失败文案，渲染为顶部 Alert，由两个处理器写入
  const [error, setError] = useState('');
  // timerRef：倒计时 setInterval 句柄，组件卸载时清理，避免卸载后 setState
  const timerRef = useRef(null);

  // useEffect（仅挂载、卸载各一次）：卸载时清除倒计时定时器（无依赖，清理函数）
  useEffect(() => () => timerRef.current && clearInterval(timerRef.current), []);

  /**
   * @function startCooldown
   * @description 启动 60 秒发送冷却：立即置 60，之后每秒减 1，到 1 后自动清除定时器
   * 被谁触发：handleSendCode 发送验证码成功后调用
   * @returns {void} 无返回值；副作用为 setInterval 与 setCooldown
   */
  const startCooldown = () => {
    setCooldown(60);
    timerRef.current = setInterval(() => {
      setCooldown((c) => {
        if (c <= 1 && timerRef.current) clearInterval(timerRef.current);
        return c - 1;
      });
    }, 1000);
  };

  /**
   * @function handleSendCode
   * @description 「发送验证码」按钮点击处理器：先校验邮箱字段，再请求发送并启动冷却
   * 被谁触发：邮箱表单内发送按钮 onClick
   * @returns {Promise<void>} 无返回值
   * @副作用 调 sendEmailCode（POST /login/email/code）；成功 message.success 并 startCooldown；
   * 校验失败不提示（antd 字段内提示），网络/业务错误 setError；最终 setSending(false)
   */
  const handleSendCode = async () => {
    setError('');
    try {
      const { email } = await form.validateFields(['email']);
      setSending(true);
      await sendEmailCode(email);
      message.success('验证码已发送，请查收邮箱');
      startCooldown();
    } catch (e) {
      // validateFields 抛校验错误对象（非真异常）；网络/业务错误展示 message
      if (e && e.message) setError(e.message);
    } finally {
      setSending(false);
    }
  };

  /**
   * @function onFinish（EmailLoginForm）
   * @description 邮箱验证码登录提交处理器，由登录按钮 / 表单回车触发（antd Form onFinish）
   * @param {{email:string, code:string}} values 表单值，来自用户输入，必填（rules 已校验）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 loginEmail（POST /login/email）；成功后按角色 navigate（规则同账号登录）；
   * 失败 setError；最终 setLoading(false)
   */
  const onFinish = async (values) => {
    setLoading(true);
    setError('');
    try {
      await loginEmail(values.email, values.code);
      const home = roleHome(useAuthStore.getState().role);
      navigate(home === '/chat' ? location.state?.from || home : home, { replace: true });
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Form form={form} layout="vertical" onFinish={onFinish} requiredMark={false}>
      {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
      <Form.Item name="email" label="邮箱" rules={RULES.email}>
        <Input placeholder="邮箱" autoComplete="email" />
      </Form.Item>
      <Form.Item label="验证码" required>
        <Space.Compact style={{ width: '100%' }}>
          <Form.Item name="code" noStyle rules={RULES.code}>
            <Input placeholder="验证码" maxLength={8} autoComplete="one-time-code" />
          </Form.Item>
          <Button onClick={handleSendCode} loading={sending} disabled={cooldown > 0}>
            {cooldown > 0 ? `${cooldown}s` : '发送验证码'}
          </Button>
        </Space.Compact>
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={loading}>
        登录
      </Button>
    </Form>
  );
}

/**
 * 组件：RegisterForm
 * 作用：新用户注册表单（用户名 + 密码 + 确认密码 + 邮箱）；成功后切换为成功提示，不自动登录
 * 实例化/挂载位置：作为 LoginPage「注册」Tab 的 children 渲染
 * 数据来源：useAuthStore（src/stores/authStore.js）的 register 动作；表单输入
 * 数据去向：注册提交到 POST /login/register（字段映射 user_name/user_pwd/email）；
 * 成功仅置 success 展示提示（用户手动切到登录 Tab），失败 setError 展示
 */
function RegisterForm() {
  // register：authStore 注册动作（POST /login/register）
  const register = useAuthStore((s) => s.register);
  // loading：注册请求进行中，驱动注册按钮 loading，由 onFinish 切换
  const [loading, setLoading] = useState(false);
  // error：注册失败文案（如用户名/邮箱已注册的防枚举文案），渲染为顶部 Alert
  const [error, setError] = useState('');
  // success：注册是否成功；成功后表单整体替换为成功 Alert，由 onFinish 置 true
  const [success, setSuccess] = useState(false);

  /**
   * @function onFinish（RegisterForm）
   * @description 注册表单提交处理器，由注册按钮 / 回车触发（antd Form onFinish，含两次密码一致校验）
   * @param {{username:string, password:string, confirm_password:string, email:string}} values
   * 表单值，均来自用户输入且必填（rules 校验长度、复杂度与一致性）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 register（POST /login/register）；成功 setSuccess(true) 展示引导；
   * 失败 setError；最终 setLoading(false)
   */
  const onFinish = async (values) => {
    setLoading(true);
    setError('');
    try {
      await register(values.username, values.password, values.email);
      setSuccess(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return <Alert type="success" message="注册成功" description="请切换到登录页使用新账号登录。" showIcon />;
  }

  return (
    <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
      {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
      <Form.Item name="username" label="用户名" rules={RULES.username}>
        <Input placeholder="用户名" autoComplete="username" />
      </Form.Item>
      <Form.Item name="password" label="密码" rules={RULES.registerPassword}>
        <Input.Password placeholder="至少 8 位，且同时包含字母和数字" autoComplete="new-password" />
      </Form.Item>
      <Form.Item
        name="confirm_password"
        label="确认密码"
        dependencies={['password']}
        rules={[
          { required: true, message: '请再次输入密码' },
          ({ getFieldValue }) => ({
            validator(_, value) {
              if (!value || getFieldValue('password') === value) return Promise.resolve();
              return Promise.reject(new Error('两次输入的密码不一致'));
            },
          }),
        ]}
      >
        <Input.Password placeholder="再次输入密码" autoComplete="new-password" />
      </Form.Item>
      <Form.Item name="email" label="邮箱" rules={RULES.email}>
        <Input placeholder="邮箱（用于接收验证码）" autoComplete="email" />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={loading}>
        注册
      </Button>
    </Form>
  );
}

/**
 * 组件：LoginPage（默认导出）
 * 作用：登录注册页容器：已登录访问时声明式重定向到角色首页，未登录渲染渐变背景卡片与三个 Tab
 * 实例化/挂载位置：路由 /login（src/router/index.jsx 直接渲染，非懒加载）
 * 数据来源：useAuthStore（src/stores/authStore.js）的 token、role（登录态持久化于 sessionStorage）
 * 数据去向：本组件不直接提交数据；登录/注册由内部三个表单组件完成；
 * 已登录时 <Navigate> 跳 roleHome(role)（admin → /users，user/teacher → /chat）
 */
export default function LoginPage() {
  // token：登录令牌，存在即视为已登录（authStore 启动时从 sessionStorage 恢复）
  const token = useAuthStore((s) => s.token);
  // role：当前角色，决定已登录用户的重定向目标
  const role = useAuthStore((s) => s.role);
  // 声明式重定向（禁止在渲染体中调用 navigate）：按角色回首页，
  // admin 固定 /users——硬跳 /chat 会让已登录 admin 落到 403 页
  if (token) {
    return <Navigate to={roleHome(role)} replace />;
  }
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg,#667eea 0%,#764ba2 100%)',
        padding: 20,
      }}
    >
      <Card style={{ width: 420, maxWidth: '95%' }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 24 }}>
          智能课程咨询服务
        </Typography.Title>
        <Tabs
          centered
          items={[
            { key: 'account', label: '账号登录', children: <AccountLoginForm /> },
            { key: 'email', label: '邮箱登录', children: <EmailLoginForm /> },
            { key: 'register', label: '注册', children: <RegisterForm /> },
          ]}
        />
      </Card>
    </div>
  );
}
