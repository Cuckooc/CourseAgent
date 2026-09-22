/**
 * @文件 HistoryPage.jsx
 * @作用 历史信息页（长期记忆浏览）：
 * - 时间范围切换：最近一天 / 最近一星期 / 全部；
 * - 滑动窗口分页：每页 10 个会话，倒序按最后消息时间排列；
 *   记录不足一页时全部展示，滚动到底点击「加载更多」追加下一页。
 * - 点击会话跳转到对话页并恢复该会话记录。
 * @主要成员 HistoryPage（默认导出，页面组件）；formatTime（时间格式化工具函数）；
 * 常量 PAGE_SIZE（每页条数 10）、RANGE_OPTIONS（时间范围单选项）
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /history；
 * 允许角色 user/teacher（MAIN_NAV），外层 RequireAuth + RequirePermission 守卫；
 * 渲染在 AppLayout 的 <Outlet/> 中；侧栏「历史信息」菜单导航到此
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { App, Button, Card, Empty, List, Radio, Spin, Tag, Typography } from 'antd';
import { ClockCircleOutlined, MessageOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { fetchHistoryPage } from '../api/sessionApi.js';
import { useSessionStore } from '../stores/sessionStore.js';

/** 每页会话条数（滑动窗口分页，对应后端 page_size） */
const PAGE_SIZE = 10;
/** 时间范围单选项：day=最近一天，week=最近一星期，all=全部（作为后端 range 参数） */
const RANGE_OPTIONS = [
  { label: '一天', value: 'day' },
  { label: '一星期', value: 'week' },
  { label: '全部', value: 'all' },
];

/**
 * @function formatTime
 * @description 会话时间展示工具函数：今天显示「今天 HH:mm」、昨天显示「昨天 HH:mm」、更早显示完整日期
 * 被谁触发：HistoryPage 列表 renderItem 渲染每条会话描述时同步调用（非事件）
 * @param {string} value 后端返回的时间字符串（last_message_time / created_at），可能为空或非法
 * @returns {string} 友好时间文本；空值或非法日期返回空字符串；纯函数无副作用
 */
function formatTime(value) {
  if (!value) return '';
  const d = dayjs(value);
  if (!d.isValid()) return '';
  const now = dayjs();
  if (d.isAfter(now.subtract(1, 'day'))) return `今天 ${d.format('HH:mm')}`;
  if (d.isAfter(now.subtract(2, 'day'))) return `昨天 ${d.format('HH:mm')}`;
  return d.format('YYYY-MM-DD HH:mm');
}

/**
 * 组件：HistoryPage
 * 作用：历史会话浏览页：时间范围筛选 + 分页列表 + 点击会话跳转聊天页恢复记录
 * 实例化/挂载位置：路由 /history，经 AppLayout 的 <Outlet/> 渲染
 * 数据来源：fetchHistoryPage（src/api/sessionApi.js，POST /history/list，带 range/page/page_size 参数）；
 * useSessionStore（src/stores/sessionStore.js）的 select 动作
 * 数据去向：点击会话调 sessionStore.select 写 currentSessionId（并持久化 sessionStorage），
 * 随后 navigate('/chat')，由 ChatPage 监听 id 变化加载该会话详情
 */
export default function HistoryPage() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  // selectSession：sessionStore 动作，写入并持久化当前会话 ID（跳转后 ChatPage 据此加载历史）
  const selectSession = useSessionStore((s) => s.select);

  // range：当前时间范围（day/week/all），切换即触发下方 effect 重新从第 1 页加载
  const [range, setRange] = useState('all');
  // page：当前已加载到的页码（从 1 起），「加载更多」时 +1
  const [page, setPage] = useState(1);
  // items：当前已加载会话列表（追加时 concat），渲染为 List 数据源
  const [items, setItems] = useState([]);
  // total：后端返回的会话总数，与 items.length 比较得出 hasMore
  const [total, setTotal] = useState(0);
  // loading：首次/切换范围加载中（控制整列表 Spin）；由 loadPage(append=false) 切换
  const [loading, setLoading] = useState(false);
  // loadingMore：追加下一页加载中（控制「加载更多」按钮 loading）；由 loadPage(append=true) 切换
  const [loadingMore, setLoadingMore] = useState(false);
  // rangeSeq：范围切换的序号守卫；快速切换范围时丢弃过期请求的回写，防止旧响应覆盖新列表
  const rangeSeq = useRef(0);

  // hasMore：派生值，已加载条数小于总数时展示「加载更多」
  const hasMore = items.length < total;

  /**
   * @function loadPage（useCallback 记忆化）
   * @description 拉取指定范围/页码的会话分页；append=false 为替换式首屏加载，true 为追加式加载更多
   * 被谁触发：范围切换 effect（首屏）、「加载更多」按钮 onClick
   * @param {'day'|'week'|'all'} nextRange 时间范围（来自 state range 或其下一个值）
   * @param {number} nextPage 目标页码（从 1 起）
   * @param {boolean} append 是否追加到现有列表（true=加载更多，false=重置为首屏）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 POST /history/list（fetchHistoryPage）；setItems/setTotal/setPage；
   * 通过 rangeSeq 比对丢弃过期回写；失败静默（列表留空，不弹打扰提示）
   */
  const loadPage = useCallback(
    async (nextRange, nextPage, append) => {
      const seq = rangeSeq.current;
      const setter = append ? setLoadingMore : setLoading;
      setter(true);
      try {
        const body = await fetchHistoryPage({
          range: nextRange,
          page: nextPage,
          pageSize: PAGE_SIZE,
        });
        if (seq !== rangeSeq.current) return; // 已切换范围：丢弃过期回写
        const rows = body.data || [];
        setItems((prev) => (append ? prev.concat(rows) : rows));
        setTotal(body.total || 0);
        setPage(nextPage);
      } catch {
        // 初始化加载失败：静默，列表保持空白，不打扰用户
      } finally {
        if (seq === rangeSeq.current) setter(false);
      }
    },
    [message],
  );

  /**
   * useEffect（依赖 [range, loadPage]）：切换时间范围时的重置逻辑——
   * 序号 +1 作废旧请求、清空列表与总数，再从第 1 页重新拉取
   */
  useEffect(() => {
    rangeSeq.current += 1;
    setItems([]);
    setTotal(0);
    loadPage(range, 1, false);
  }, [range, loadPage]);

  /**
   * @function handleRangeChange
   * @description 时间范围单选切换处理器；仅更新 range，实际加载由上面的 effect 统一负责
   * 被谁触发：Radio.Group 的 onChange（按钮组「一天/一星期/全部」）
   * @param {{target:{value:'day'|'week'|'all'}}} e antd Radio change 事件，value 来自 RANGE_OPTIONS
   * @returns {void} 副作用为 setRange
   */
  function handleRangeChange(e) {
    setRange(e.target.value);
  }

  /**
   * @function handleOpen
   * @description 点击会话条目：选定该会话（写 sessionStore + sessionStorage）并跳转对话页
   * 被谁触发：List.Item 的 onClick（renderItem 内绑定，参数为当前行数据）
   * @param {{session_id:number, title:string, last_message_time?:string}} item 会话行数据，来自列表接口
   * @returns {void} 副作用为 selectSession + navigate('/chat')；不直接调接口
   */
  function handleOpen(item) {
    selectSession(item.session_id);
    navigate('/chat');
  }

  return (
    <div style={{ padding: 16, maxWidth: 900, margin: '0 auto' }}>
      <Card
        title="历史信息"
        extra={
          <Radio.Group
            options={RANGE_OPTIONS}
            value={range}
            onChange={handleRangeChange}
            optionType="button"
            buttonStyle="solid"
          />
        }
      >
        {loading && !items.length ? (
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin />
          </div>
        ) : !items.length ? (
          <Empty
            description={
              range === 'all'
                ? '暂无历史会话，开始一次课程咨询吧'
                : `最近${range === 'day' ? '一天' : '一星期'}没有历史会话`
            }
          />
        ) : (
          <>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {total} 个会话，按最后对话时间倒序
            </Typography.Text>
            <List
              itemLayout="horizontal"
              dataSource={items}
              style={{ marginTop: 8 }}
              renderItem={(item) => {
                const time = item.last_message_time || item.created_at;
                return (
                  <List.Item
                    onClick={() => handleOpen(item)}
                    style={{ cursor: 'pointer', borderRadius: 8, padding: '12px 12px' }}
                  >
                    <List.Item.Meta
                      avatar={<MessageOutlined style={{ fontSize: 18, color: '#1677ff' }} />}
                      title={
                        <span style={{ color: 'inherit' }} title={item.title}>
                          {item.title || '未命名会话'}
                        </span>
                      }
                      description={
                        <span style={{ fontSize: 12 }}>
                          <ClockCircleOutlined /> {formatTime(time)}
                        </span>
                      }
                    />
                    {item.last_message_time ? <Tag color="blue">最近对话</Tag> : null}
                  </List.Item>
                );
              }}
            />
            <div style={{ textAlign: 'center', marginTop: 12 }}>
              {hasMore ? (
                <Button
                  loading={loadingMore}
                  onClick={() => loadPage(range, page + 1, true)}
                >
                  加载更多（已显示 {items.length}/{total}）
                </Button>
              ) : (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  已全部加载（{items.length} 个会话）
                </Typography.Text>
              )}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
