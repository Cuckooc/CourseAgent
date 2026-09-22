/**
 * @文件 UsagePage.jsx
 * @作用 LLM 用量看板（admin 专属）：
 * 1. 按模型聚合的 requests / prompt_tokens / completion_tokens
 * 2. 按用户聚合的月度 token 消耗表（支持切换月份）
 * 限流 30 次/分钟，仅手动刷新不做自动轮询。
 * @主要成员 UsagePage（默认导出，页面组件）；工具函数 formatTime（时分秒格式化）、
 * totalTokens（单行总 token）、compactNumber（大数字紧凑显示）；
 * 常量 ROLE_LABEL、ROLE_COLOR（用户表角色展示映射）
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /usage；
 * 仅允许角色 admin（MAIN_NAV），外层 RequireAuth + RequirePermission 守卫，其他角色直达渲染 403；
 * 渲染在 AppLayout 的 <Outlet/> 中；侧栏「用量统计」菜单导航到此
 */
import { useEffect, useMemo, useState } from 'react';
import {
  App,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import { ReloadOutlined, UserOutlined } from '@ant-design/icons';
import { useUsageStore } from '../stores/usageStore.js';

/**
 * @function formatTime
 * @description 将 Date 格式化为「HH:mm:ss」，用于「最近刷新」时刻展示
 * 被谁触发：UsagePage 渲染卡片右上角时同步调用（非事件）
 * @param {Date|null} date 最近成功刷新时间（usageStore.lastFetched），可能为 null
 * @returns {string} 两位补零的时分秒；空值返回「—」；纯函数无副作用
 */
function formatTime(date) {
  if (!date) return '—';
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 月度用户表角色 → 中文标签映射（后端当前仅 admin/user 两类） */
const ROLE_LABEL = { admin: '管理员', user: '普通用户' };
/** 月度用户表角色 → Tag 颜色映射 */
const ROLE_COLOR = { admin: 'purple', user: 'default' };

/**
 * @function totalTokens
 * @description 计算单行用量的总 token = prompt_tokens + completion_tokens
 * 被谁触发：表格「总 Tokens」列 render 与各列 sorter 排序时同步调用
 * @param {{prompt_tokens?:number, completion_tokens?:number}} r 月度用户用量行
 * @returns {number} 总 token（缺省字段按 0 计）；纯函数无副作用
 */
function totalTokens(r) {
  return (r.prompt_tokens || 0) + (r.completion_tokens || 0);
}

/**
 * @function compactNumber
 * @description 大数字紧凑格式化：≥100 万显示 x.xxM，≥1 万显示 x.xK，其余千分位
 * 被谁触发：模型卡片 Statistic 的 formatter（antd 回调，参数为当前数值）
 * @param {number|string} v 待格式化数值（token 数）
 * @returns {string} 紧凑数字字符串；非数值按 0 处理；纯函数无副作用
 */
function compactNumber(v) {
  const n = Number(v) || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 10_000) return (n / 1_000).toFixed(1) + 'K';
  return n.toLocaleString();
}

/**
 * 组件：UsagePage
 * 作用：LLM 用量看板：上半区按模型聚合指标卡片，下半区按用户聚合的月度消耗表（含合计行与排序）
 * 实例化/挂载位置：路由 /usage，经 AppLayout 的 <Outlet/> 渲染（仅 admin）
 * 数据来源：useUsageStore（Zustand，src/stores/usageStore.js）：
 * usage/lastFetched/loading/fetchUsage（GET /admin/llm/usage）与
 * userUsageRows/userUsageMonth/userLoading/fetchUserUsage（GET /admin/llm/usage/users?month=）
 * 数据去向：本页只读不写；所有请求由「刷新」按钮或月份选择器手动触发（不轮询，规避 30 次/分限流）
 */
export default function UsagePage() {
  const { message } = App.useApp();
  // usage：按模型聚合的快照 {model:{requests,prompt_tokens,completion_tokens}}（usageStore）
  const usage = useUsageStore((s) => s.usage);
  // lastFetched：模型用量最近成功刷新时间（Date），右上角展示
  const lastFetched = useUsageStore((s) => s.lastFetched);
  // loading：模型用量请求中，驱动卡片区 Spin 与刷新按钮
  const loading = useUsageStore((s) => s.loading);
  // fetchUsage：拉取按模型聚合用量的 store 动作
  const fetchUsage = useUsageStore((s) => s.fetchUsage);

  // userUsageRows：所选月份按用户聚合的用量行（月度表 dataSource）
  const userUsageRows = useUsageStore((s) => s.userUsageRows);
  // userUsageMonth：当前已加载月份（YYYY-MM，后端回传，合计行标题展示）
  const userUsageMonth = useUsageStore((s) => s.userUsageMonth);
  // userLoading：月度用户用量请求中，驱动表格 loading
  const userLoading = useUsageStore((s) => s.userLoading);
  // fetchUserUsage：按月份拉取用户用量的 store 动作（缺省当月）
  const fetchUserUsage = useUsageStore((s) => s.fetchUserUsage);

  // month：月份选择器的 dayjs 值（null=未选择，请求默认当月）；仅控制选择器显示
  const [month, setMonth] = useState(null); // dayjs | null → 默认当月

  /**
   * useEffect（依赖 []，仅挂载一次）：并发拉取模型用量与当月用户用量；
   * 首屏失败静默（catch 内不弹提示），用户可手动刷新
   */
  useEffect(() => {
    fetchUsage().catch(() => { /* 初始化加载失败：静默 */ });
    fetchUserUsage().catch(() => { /* 初始化加载失败：静默 */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // models：派生值——快照中的模型名排序后用于渲染模型卡片
  const models = Object.keys(usage).sort();

  /**
   * columns（useMemo 依赖 []，纯静态列配置）：月度用户用量表列——
   * 用户（姓名+ID 复制+角色 Tag）/ 邮箱 / 请求数 / Prompt / Completion / 总 Tokens；
   * 数值列均带 sorter，请求数默认降序
   */
  const columns = useMemo(
    () => [
      {
        title: '用户',
        dataIndex: 'user_name',
        render: (name, row) => (
          <Space>
            <UserOutlined />
            <Typography.Text copyable={{ text: String(row.user_id) }}>
              {name}
            </Typography.Text>
            <Tag color={ROLE_COLOR[row.role] || 'default'}>
              {ROLE_LABEL[row.role] || row.role}
            </Tag>
          </Space>
        ),
      },
      {
        title: '邮箱',
        dataIndex: 'email',
        render: (e) => <Typography.Text type="secondary">{e || '—'}</Typography.Text>,
      },
      {
        title: '请求数',
        dataIndex: 'requests',
        sorter: (a, b) => a.requests - b.requests,
        defaultSortOrder: 'descend',
      },
      {
        title: 'Prompt Tokens',
        dataIndex: 'prompt_tokens',
        sorter: (a, b) => a.prompt_tokens - b.prompt_tokens,
      },
      {
        title: 'Completion Tokens',
        dataIndex: 'completion_tokens',
        sorter: (a, b) => a.completion_tokens - b.completion_tokens,
      },
      {
        title: '总 Tokens',
        key: 'total',
        render: (_, row) => (
          <Typography.Text strong>{totalTokens(row).toLocaleString()}</Typography.Text>
        ),
        sorter: (a, b) => totalTokens(a) - totalTokens(b),
      },
    ],
    [],
  );

  /**
   * @function handleRefresh
   * @description 「刷新」按钮处理器：并发手动刷新模型用量与当前月份用户用量
   * 被谁触发：卡片右上角「刷新」按钮 onClick
   * @returns {void} 无返回值
   * @副作用 调 GET /admin/llm/usage 与 GET /admin/llm/usage/users（月份沿用 store 当前值）；
   * 失败分别 message.error 提示（手动刷新失败不静默）
   */
  function handleRefresh() {
    fetchUsage().catch((e) => message.error(e.message || '刷新失败'));
    fetchUserUsage(userUsageMonth || undefined).catch((e) =>
      message.error(e.message || '刷新用户用量失败'),
    );
  }

  /**
   * @function handleMonthChange
   * @description 月份选择器变更处理器：更新本地选择值并拉取所选月份用户用量（清空则回到当月）
   * 被谁触发：DatePicker 的 onChange（allowClear，清空时 d=null）
   * @param {null|object} d dayjs 月份对象或 null（清空）
   * @returns {void} 无返回值
   * @副作用 setMonth；调 GET /admin/llm/usage/users?month=YYYY-MM（fetchUserUsage）；失败 message.error
   */
  function handleMonthChange(d) {
    setMonth(d);
    const m = d ? d.format('YYYY-MM') : undefined;
    fetchUserUsage(m).catch((e) => message.error(e.message || '切换月份失败'));
  }

  return (
    <div style={{ padding: 16, maxWidth: 1100, margin: '0 auto' }}>
      <Card
        title="LLM 用量看板"
        extra={
          <Space>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              最近刷新：{formatTime(lastFetched)}
            </Typography.Text>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={handleRefresh}>
              刷新
            </Button>
          </Space>
        }
      >
        <Typography.Title level={5} style={{ marginTop: 0 }}>
          按模型聚合
        </Typography.Title>
        {loading && !models.length ? (
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin />
          </div>
        ) : !models.length ? (
          <Typography.Text type="secondary">暂无用量数据（发起对话后此处按模型统计）</Typography.Text>
        ) : (
          <Row gutter={[16, 16]}>
            {models.map((model) => {
              const m = usage[model];
              return (
                <Col xs={24} sm={12} lg={8} key={model}>
                  <Card type="inner" title={<Typography.Text code>{model}</Typography.Text>}>
                    <Row gutter={8}>
                      <Col span={8} style={{ textAlign: 'center' }}>
                        <Statistic
                          title="请求数"
                          value={m.requests || 0}
                          valueStyle={{ fontSize: 18 }}
                        />
                      </Col>
                      <Col span={8} style={{ textAlign: 'center' }}>
                        <Statistic
                          title="Prompt Tokens"
                          value={m.prompt_tokens || 0}
                          formatter={compactNumber}
                          valueStyle={{ fontSize: 18 }}
                        />
                      </Col>
                      <Col span={8} style={{ textAlign: 'center' }}>
                        <Statistic
                          title="Completion Tokens"
                          value={m.completion_tokens || 0}
                          formatter={compactNumber}
                          valueStyle={{ fontSize: 18 }}
                        />
                      </Col>
                    </Row>
                  </Card>
                </Col>
              );
            })}
          </Row>
        )}
      </Card>

      <Card
        title="按用户月度消耗"
        style={{ marginTop: 16 }}
        extra={
          <DatePicker
            picker="month"
            allowClear
            value={month}
            placeholder="选择月份"
            format="YYYY-MM"
            onChange={handleMonthChange}
          />
        }
      >
        <Table
          rowKey="user_id"
          size="middle"
          loading={userLoading}
          dataSource={userUsageRows}
          columns={columns}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          locale={{ emptyText: <Empty description="该月份暂无用户用量记录" /> }}
          summary={(rows) => {
            const requests = rows.reduce((s, r) => s + (r.requests || 0), 0);
            const prompt = rows.reduce((s, r) => s + (r.prompt_tokens || 0), 0);
            const completion = rows.reduce((s, r) => s + (r.completion_tokens || 0), 0);
            return (
              <Table.Summary fixed>
                <Table.Summary.Row>
                  <Table.Summary.Cell index={0} colSpan={2}>
                    <Typography.Text strong>合计（{userUsageMonth}）</Typography.Text>
                  </Table.Summary.Cell>
                  <Table.Summary.Cell index={2}>{requests.toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={3}>{prompt.toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={4}>{completion.toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={5}>
                    <Typography.Text strong>{(prompt + completion).toLocaleString()}</Typography.Text>
                  </Table.Summary.Cell>
                </Table.Summary.Row>
              </Table.Summary>
            );
          }}
        />
      </Card>
    </div>
  );
}
