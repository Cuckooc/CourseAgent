/**
 * @文件 ProfilePage.jsx
 * @作用 个人信息页：用户画像（长期记忆）。
 * - 画像由系统从历史对话自动提取（常问内容、兴趣爱好），用于优化后续回答；
 * - 用户可随时手动修改：修改先暂存，连续 7 天没有再次更新才正式入库，
 *   期间读取以暂存内容为准；未修改则沿用原画像。
 * @主要成员 ProfilePage（默认导出，页面组件）；常量 LIMITS（三个画像字段的最大长度）
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /account
 * （刻意避开 /profile，防止整页刷新直连命中后端画像 API 返回 401 JSON 白屏）；
 * 允许角色 user/teacher（MAIN_NAV），外层 RequireAuth + RequirePermission 守卫；
 * 渲染在 AppLayout 的 <Outlet/> 中；侧栏底部头像/用户名点击导航到此（admin 无此页）
 */
import { useEffect, useState } from 'react';
import {
  App,
  Avatar,
  Button,
  Card,
  Col,
  Form,
  Input,
  Row,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { UserOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { fetchProfile, updateProfile } from '../api/profileApi.js';
import { useAuthStore } from '../stores/authStore.js';

/** 画像字段长度上限（字符）：profile_text 画像要点 4000，interests/topics 各 500 */
const LIMITS = { profile_text: 4000, interests: 500, topics: 500 };

/**
 * 组件：ProfilePage
 * 作用：个人信息 / 用户画像页：挂载拉取画像回填表单，支持手动保存（后端 7 天暂存规则）
 * 实例化/挂载位置：路由 /account，经 AppLayout 的 <Outlet/> 渲染
 * 数据来源：fetchProfile（src/api/profileApi.js，GET /profile，返回 MySQL 基线 + Redis 暂存）；
 * useAuthStore（src/stores/authStore.js）的 userName
 * 数据去向：handleSave 调 updateProfile（PUT /profile）整表提交三字段，
 * 后端仅写 Redis 暂存并重置 7 天计时，返回 pending/due_at 由页面展示
 */
export default function ProfilePage() {
  const { message } = App.useApp();
  // userName：当前登录用户名（authStore），仅用于页头展示
  const userName = useAuthStore((s) => s.userName);
  // form：antd Form 实例，挂载拉取成功后 setFieldsValue 回填，保存时经 onFinish 取值
  const [form] = Form.useForm();

  // loading：画像首屏加载中，控制表单区 Spin；由挂载 effect 切换
  const [loading, setLoading] = useState(true);
  // saving：保存请求进行中，控制保存按钮 loading；由 handleSave 切换
  const [saving, setSaving] = useState(false);
  // meta：暂存元信息 {pending, due_at}；pending=true 表示 7 天暂存中，due_at 为正式入库时间（unix 秒）
  const [meta, setMeta] = useState({ pending: false, due_at: null });

  /**
   * useEffect（依赖 []，仅挂载一次）：拉取当前用户画像并回填表单。
   * 副作用：GET /profile（fetchProfile）；setFieldsValue、setMeta、setLoading。
   * 清理函数置 alive=false，防止卸载后异步回写 setState；加载失败静默（表单留空）
   */
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchProfile()
      .then((data) => {
        if (!alive) return;
        form.setFieldsValue({
          profile_text: data.profile_text || '',
          interests: data.interests || '',
          topics: data.topics || '',
        });
        setMeta({ pending: !!data.pending, due_at: data.due_at || null });
      })
      .catch(() => {
        // 初始化加载失败：静默，表单保持空白，不打扰用户
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * @function handleSave
   * @description 画像保存处理器：三字段 trim 后整表提交，成功提示并更新暂存元信息
   * 被谁触发：Form onFinish——点击「保存画像」按钮或表单回车
   * @param {{profile_text?:string, interests?:string, topics?:string}} values
   * 表单值（来自用户编辑，字段均可空；长度已由 maxLength/showCount 限制）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 PUT /profile（updateProfile）；message.success、setMeta、setSaving；失败 message.error
   */
  async function handleSave(values) {
    setSaving(true);
    try {
      const data = await updateProfile({
        profile_text: (values.profile_text || '').trim(),
        interests: (values.interests || '').trim(),
        topics: (values.topics || '').trim(),
      });
      message.success('画像已更新');
      setMeta({ pending: !!data.pending, due_at: data.due_at || null });
    } catch (e) {
      message.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  }

  // dueText：派生展示值——将暂存到期 unix 秒（meta.due_at）格式化为「YYYY-MM-DD HH:mm」，无则 null
  const dueText = meta.due_at
    ? dayjs.unix(Math.floor(meta.due_at)).format('YYYY-MM-DD HH:mm')
    : null;

  return (
    <div style={{ padding: 16, maxWidth: 820, margin: '0 auto' }}>
      <Card title="个人信息">
        <Form form={form} layout="vertical" onFinish={handleSave}>
          {loading ? (
            <div style={{ textAlign: 'center', padding: 48 }}>
              <Spin />
            </div>
          ) : (
            <>
              <Row gutter={16} align="middle" style={{ marginBottom: 8 }}>
                <Col>
                  <Avatar size="large" icon={<UserOutlined />} />
                </Col>
                <Col>
                  <Typography.Title level={5} style={{ margin: 0 }}>
                    {userName || '用户'}
                  </Typography.Title>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    画像会作为背景信息随你的提问提供给模型，让回答更贴合你的习惯
                  </Typography.Text>
                </Col>
              </Row>

              <Card
                size="small"
                type="inner"
                title="用户画像"
                style={{ marginTop: 8 }}
                extra={
                  meta.pending ? (
                    <Tag color="processing">
                      近期有修改{dueText ? `，${dueText} 后正式入库` : '，7 天后正式入库'}
                    </Tag>
                  ) : (
                    <Tag>已入库</Tag>
                  )
                }
              >
                <Form.Item
                  label="画像要点（学习/工作领域、表达习惯、关注重点）"
                  name="profile_text"
                  extra="系统会根据你的对话自动提取并合并，你也可以手动修改。"
                >
                  <Input.TextArea
                    rows={6}
                    showCount
                    maxLength={LIMITS.profile_text}
                    placeholder="例如：正在准备考研；偏好先给结论再举例说明；关注课程的实践性…"
                  />
                </Form.Item>
                <Row gutter={16}>
                  <Col xs={24} sm={12}>
                    <Form.Item label="兴趣爱好" name="interests">
                      <Input showCount maxLength={LIMITS.interests} placeholder="如：阅读、篮球、编程" />
                    </Form.Item>
                  </Col>
                  <Col xs={24} sm={12}>
                    <Form.Item label="常问主题" name="topics">
                      <Input showCount maxLength={LIMITS.topics} placeholder="如：高数、考研英语、Python" />
                    </Form.Item>
                  </Col>
                </Row>
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
                  保存规则：修改先暂存 7 天，期间如再次修改将重新计时；连续 7 天没有更新才写入正式资料。
                </Typography.Paragraph>
                <Button type="primary" htmlType="submit" loading={saving}>
                  保存画像
                </Button>
              </Card>
            </>
          )}
        </Form>
      </Card>
    </div>
  );
}
