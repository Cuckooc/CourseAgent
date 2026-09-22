/**
 * @文件 ReviewPage.jsx
 * @作用 文档审核页：
 * - 我的审核（所有角色）：按状态分 Tab（待审核/已通过/已驳回）分页查看自己的上传文档
 * - 全部审核（仅 teacher/admin 审核员）：全量队列，可按上传用户 ID 筛选并代审
 * - 详情：查看原始文本与清洗后文本，支持编辑后通过或驳回
 * - 审核通过后自动触发知识库入库（文档写入上传者私有库）
 * @主要成员 ReviewPage（默认导出，页面组件）；常量 STATUS_MAP（审核状态展示映射）、
 * DOC_TYPE_MAP（文档类型展示映射）；内部函数 loadList、switchScope、applyFilter、
 * openDetail、handleApprove、handleReject，以及列配置 columns 与 Tab 配置 tabItems
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /review；
 * 允许角色 user/teacher/admin（MAIN_NAV），外层 RequireAuth + RequirePermission 守卫；
 * 渲染在 AppLayout 的 <Outlet/> 中；侧栏「文档审核」菜单导航到此
 */
import { useCallback, useEffect, useState } from 'react';
import {
  App,
  Button,
  Card,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import {
  CheckOutlined,
  CloseOutlined,
  EditOutlined,
  EyeOutlined,
} from '@ant-design/icons';
import { useAuthStore } from '../stores/authStore.js';
import {
  approveReview,
  fetchAllReviewList,
  fetchReviewDetail,
  fetchReviewList,
  rejectReview,
} from '../api/reviewApi.js';

const { TextArea } = Input;
const { Text } = Typography;

/** 审核状态 → 标签文案 / Tag 颜色映射（pending 待审核 / approved 已通过 / rejected 已驳回） */
const STATUS_MAP = {
  pending: { label: '待审核', color: 'processing' },
  approved: { label: '已通过', color: 'success' },
  rejected: { label: '已驳回', color: 'error' },
};

/** 文档类型 → 中文文案映射（后端 doc_type：扫描件 OCR / 图文混合 / 双栏 / 纯文本） */
const DOC_TYPE_MAP = {
  scanned: '扫描件 OCR',
  image_rich: '图文混合',
  two_column: '双栏文档',
  pure_text: '纯文本',
};

/**
 * 组件：ReviewPage
 * 作用：文档审核工作台：我的/全量队列切换 + 状态 Tab + 分页表格 + 详情弹窗（可编辑清洗文本后通过/驳回）
 * 实例化/挂载位置：路由 /review，经 AppLayout 的 <Outlet/> 渲染
 * 数据来源：authStore（src/stores/authStore.js）的 role（决定是否展示全量队列）；
 * reviewApi（src/api/reviewApi.js）：fetchReviewList（GET /review/list 我的）、
 * fetchAllReviewList（GET /review/all 全量，teacher/admin）、fetchReviewDetail、approveReview、rejectReview
 * 数据去向：通过/驳回经 modal.confirm 二次确认后 POST 到对应接口；
 * 通过携带可选 edited_text（后端写入文档上传者私有库），驳回携带 notes 原因；完成后刷新当前列表
 */
export default function ReviewPage() {
  const { message, modal } = App.useApp();
  // role：当前用户角色（authStore）；teacher/admin 为审核员，可见「全部审核」队列
  const role = useAuthStore((s) => s.role);
  const isModerator = role === 'teacher' || role === 'admin';
  // scope：列表视图，mine=我的审核（/review/list），all=全部审核（/review/all，仅审核员）
  const [scope, setScope] = useState('mine');
  // activeTab：当前状态 Tab（pending/approved/rejected），切换即触发 loadList effect
  const [activeTab, setActiveTab] = useState('pending');
  // filterUserIdInput：全量队列筛选输入框草稿（上传者 ID，null=不限）
  const [filterUserIdInput, setFilterUserIdInput] = useState(null);
  // appliedUserId：已生效的上传者 ID 过滤（点「查询」后才应用，避免输入中途频繁请求）
  const [appliedUserId, setAppliedUserId] = useState(null);
  // rows：当前 Tab 的审核记录页数据，作为 Table dataSource
  const [rows, setRows] = useState([]);
  // total：当前状态下记录总数，驱动分页器
  const [total, setTotal] = useState(0);
  // page：当前页码（后端回传为准），分页器 onChange 时变化
  const [page, setPage] = useState(1);
  // loading：列表加载中，驱动 Table loading；由 loadList 切换
  const [loading, setLoading] = useState(false);
  // detailOpen：详情弹窗是否打开；由 openDetail 置 true、关闭/审核完成置 false
  const [detailOpen, setDetailOpen] = useState(false);
  // detail：当前详情数据（GET /review/{id} 返回），null 时弹窗内不渲染内容
  const [detail, setDetail] = useState(null);
  // detailLoading：详情加载中，弹窗内显示「加载中…」；由 openDetail 切换
  const [detailLoading, setDetailLoading] = useState(false);
  // editedText：弹窗内可编辑的清洗后文本草稿，初始化为 detail.cleaned_text
  const [editedText, setEditedText] = useState('');
  // actionLoading：通过/驳回请求进行中，驱动弹窗底部按钮 loading，防重复提交
  const [actionLoading, setActionLoading] = useState(false);

  // pageSize：固定每页 20 条（与后端默认 page_size 对齐）
  const pageSize = 20;

  /**
   * @function loadList（useCallback 记忆化）
   * @description 按视图 + 状态 + 页码拉取审核记录分页并写入表格：
   * scope=mine 调 GET /review/list（仅本人）；scope=all 调 GET /review/all（全量，带 user_id 过滤）
   * 被谁触发：视图/ Tab/ 过滤条件切换 effect（回到第 1 页）；分页器 onChange；审核完成后刷新
   * @param {'pending'|'approved'|'rejected'} status 审核状态，来自 activeTab
   * @param {number} [p=1] 目标页码，默认第 1 页
   * @returns {Promise<void>} 无返回值
   * @副作用 调 reviewApi 列表接口；setRows/setTotal/setPage/setLoading；失败静默留空
   */
  const loadList = useCallback(
    async (status, p = 1) => {
      setLoading(true);
      try {
        const res =
          scope === 'all'
            ? await fetchAllReviewList({
                status,
                page: p,
                page_size: pageSize,
                userId: appliedUserId,
              })
            : await fetchReviewList({ status, page: p, page_size: pageSize });
        setRows(res.data);
        setTotal(res.total);
        setPage(res.page);
      } catch {
        // 初始化 / 切换视图或 Tab 加载失败：静默，列表保持空白，不打扰用户
      } finally {
        setLoading(false);
      }
    },
    [scope, appliedUserId]
  );

  // useEffect（依赖 [scope, activeTab, appliedUserId, loadList]）：切换视图/状态/过滤条件时回到第 1 页加载
  useEffect(() => {
    loadList(activeTab, 1);
  }, [scope, activeTab, appliedUserId, loadList]);

  /**
   * @function switchScope
   * @description 切换「我的审核 / 全部审核」视图：重置状态 Tab、上传者过滤与输入草稿，
   * 再由 scope 变化触发的 effect 重新加载（仅审核员能切到 all）。
   * 被谁触发：视图 Segmented onChange。
   * @param {'mine'|'all'} nextScope 目标视图 key。
   * @returns {void}
   */
  function switchScope(nextScope) {
    if (nextScope === scope) return;
    setScope(nextScope);
    setActiveTab('pending');
    setAppliedUserId(null);
    setFilterUserIdInput(null);
  }

  /**
   * @function applyFilter
   * @description 应用全量队列的上传者 ID 过滤：把输入框草稿写入 appliedUserId
   * （effect 监听到变化后自动回到第 1 页重新查询）。
   * 被谁触发：全量视图「查询」按钮；「清除」链接以 null 调用以恢复全部用户。
   * @param {number|null} userId 上传者 ID；null 或非正数表示不限。
   * @returns {void}
   */
  function applyFilter(userId) {
    setAppliedUserId(userId && userId > 0 ? userId : null);
  }

  /**
   * @function openDetail
   * @description 打开审核详情弹窗并拉取单条详情，用清洗后文本初始化编辑草稿。
   *   「我的审核」打开本人记录，「全部审核」打开任意用户记录（后端 _can_moderate
   *   允许 teacher/admin 代审，普通用户打开他人记录会收到 403）。
   * 被谁触发：表格操作列「查看」按钮 onClick（render 内绑定 row.id，两个视图共用）
   * @param {number|string} reviewId 审核记录 ID（行数据 id），拼入 GET /review/{id}
   * @returns {Promise<void>} 无返回值
   * @副作用 setDetailOpen(true)、setDetailLoading；成功 setDetail/setEditedText；
   * 失败（403 越权/404 不存在）message.error 并关闭弹窗
   */
  async function openDetail(reviewId) {
    setDetailLoading(true);
    setDetailOpen(true);
    try {
      const data = await fetchReviewDetail(reviewId);
      setDetail(data);
      setEditedText(data.cleaned_text || '');
    } catch (e) {
      message.error(e.message || '加载审核详情失败');
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }

  /**
   * @function handleApprove
   * @description 「通过并入库」处理器：对比草稿与原文判断是否编辑，弹确认框后提交通过；
   * 仅当文本被修改时才回传 edited_text。全部队列代审他人文档时，后端会把知识写入
   * 文档上传者的私有库（不是审核者库），弹窗标题的「上传者 ID」标签可核对归属。
   * 被谁触发：详情弹窗底部「通过并入库」按钮 onClick（仅 pending 状态显示；本人与代审共用）
   * @returns {void} 无入参（使用组件内 detail/editedText）
   * @副作用 onOk 内调 POST /review/{id}/approve（approveReview，
   * 数据来源：detail 来自 GET /review/{id}、editedText 来自本页编辑草稿）；
   * 成功 message.success、关弹窗并 loadList 刷新当前视图/页；失败 message.error；setActionLoading 防重入
   */
  function handleApprove() {
    if (!detail) return;
    const isEdited = editedText !== (detail.cleaned_text || '');
    modal.confirm({
      title: '确认审核通过？',
      content: isEdited
        ? '文本已修改，通过后将以编辑后的文本入库到知识库。'
        : '通过后将文本入库到知识库。',
      okText: '通过并入库',
      cancelText: '取消',
      async onOk() {
        setActionLoading(true);
        try {
          await approveReview(detail.id, {
            edited_text: isEdited ? editedText : null,
          });
          message.success('审核通过，已入库知识库');
          setDetailOpen(false);
          loadList(activeTab, page);
        } catch (e) {
          message.error(e.message || '审核通过失败');
        } finally {
          setActionLoading(false);
        }
      },
    });
  }

  /**
   * @function handleReject
   * @description 「驳回」处理器：弹出含驳回原因输入框的确认框；原因为空则阻止关闭并警告，
   * 填写后提交驳回（仅状态流转，不触发向量入库；本人与 teacher/admin 代审共用此入口）。
   * 被谁触发：详情弹窗底部「驳回」按钮 onClick（仅 pending 状态显示）
   * @returns {void} 无入参（使用组件内 detail；原因在确认框内即时输入）
   * @副作用 onOk 内调 POST /review/{id}/reject（rejectReview，携带 notes，
   * 数据来源：确认框内非受控 TextArea 的 DOM 值）；
   * 成功 message.success、关弹窗并 loadList 刷新当前视图/页；失败 message.error
   */
  function handleReject() {
    if (!detail) return;
    modal.confirm({
      title: '确认驳回？',
      content: (
        <div>
          <Text>请输入驳回原因：</Text>
          <TextArea
            id="reject-notes-input"
            rows={3}
            placeholder="请说明驳回原因…"
            style={{ marginTop: 8 }}
          />
        </div>
      ),
      okText: '驳回',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        // 驳回原因输入框是 modal.confirm content 内的非受控 TextArea：
        // 通过固定 id 直接读 DOM 值，避免为一次性确认框引入额外 state
        const notesEl = document.getElementById('reject-notes-input');
        const notes = notesEl?.value?.trim();
        if (!notes) {
          message.warning('请填写驳回原因');
          return Promise.reject();
        }
        setActionLoading(true);
        try {
          await rejectReview(detail.id, notes);
          message.success('已驳回');
          setDetailOpen(false);
          loadList(activeTab, page);
        } catch (e) {
          message.error(e.message || '驳回失败');
        } finally {
          setActionLoading(false);
        }
      },
    });
  }

  /**
   * columns：审核记录表格列配置（渲染辅助）：
   * 文件名 / [全量视图额外：上传者 ID] / 文档类型（DOC_TYPE_MAP 转义）/
   * 状态（STATUS_MAP 转 Tag）/ 提交时间 / 操作（查看 → openDetail）
   */
  const columns = [
    {
      title: '文件名',
      dataIndex: 'file_name',
      ellipsis: true,
    },
    // 全部审核队列跨用户展示，需要上传者列；我的审核记录均为本人，插入空列反而占位
    ...(scope === 'all'
      ? [
          {
            title: '上传者 ID',
            dataIndex: 'user_id',
            width: 100,
          },
        ]
      : []),
    {
      title: '文档类型',
      dataIndex: 'doc_type',
      width: 120,
      render: (t) => DOC_TYPE_MAP[t] || t,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (s) => {
        const info = STATUS_MAP[s] || { label: s, color: 'default' };
        return <Tag color={info.color}>{info.label}</Tag>;
      },
    },
    {
      title: '提交时间',
      dataIndex: 'created_at',
      width: 180,
      render: (t) => (t ? new Date(t).toLocaleString('zh-CN') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      render: (_, row) => (
        <Button size="small" icon={<EyeOutlined />} onClick={() => openDetail(row.id)}>
          查看
        </Button>
      ),
    },
  ];

  /** tabItems：顶部状态 Tab 配置（key 作为 activeTab 与查询参数 status） */
  const tabItems = [
    { key: 'pending', label: '待审核' },
    { key: 'approved', label: '已通过' },
    { key: 'rejected', label: '已驳回' },
  ];

  return (
    <div style={{ padding: 16, maxWidth: 1100, margin: '0 auto' }}>
      <Card
        title={scope === 'all' ? '文档审核 · 全部队列' : '文档审核'}
        styles={{ body: { paddingTop: 4 } }}
      >
        {/* 视图切换：仅 teacher/admin 审核员可见「全部审核」队列 */}
        {isModerator && (
          <div style={{ marginBottom: 12 }}>
            <Segmented
              value={scope}
              onChange={switchScope}
              options={[
                { label: '我的审核', value: 'mine' },
                { label: '全部审核', value: 'all' },
              ]}
            />
          </div>
        )}
        {/* 全量队列按上传用户 ID 精确过滤（回车或点查询生效；appliedUserId 非空时可清除） */}
        {scope === 'all' && (
          <Space style={{ marginBottom: 12 }}>
            <span>上传者 ID：</span>
            <InputNumber
              min={1}
              precision={0}
              placeholder="不限"
              value={filterUserIdInput}
              onChange={setFilterUserIdInput}
              onPressEnter={() => applyFilter(filterUserIdInput)}
              style={{ width: 140 }}
            />
            <Button type="primary" onClick={() => applyFilter(filterUserIdInput)}>
              查询
            </Button>
            {appliedUserId && (
              <Button
                type="link"
                onClick={() => {
                  setFilterUserIdInput(null);
                  applyFilter(null);
                }}
              >
                清除筛选
              </Button>
            )}
          </Space>
        )}
        <Tabs
          activeKey={activeTab}
          onChange={(key) => setActiveTab(key)}
          items={tabItems}
          style={{ marginBottom: 8 }}
        />
        <Table
          rowKey="id"
          size="middle"
          loading={loading}
          dataSource={rows}
          columns={columns}
          pagination={{
            current: page,
            pageSize,
            total,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p) => loadList(activeTab, p),
          }}
          locale={{ emptyText: '暂无审核记录' }}
        />
      </Card>

      <Modal
        title={
          detail ? (
            <Space>
              <span>审核详情</span>
              {scope === 'all' && detail?.user_id != null && (
                <Tag color="default">上传者 ID：{detail.user_id}</Tag>
              )}
              <Tag color={STATUS_MAP[detail.status]?.color || 'default'}>
                {STATUS_MAP[detail.status]?.label || detail.status}
              </Tag>
            </Space>
          ) : '审核详情'
        }
        open={detailOpen}
        onCancel={() => setDetailOpen(false)}
        width={800}
        footer={
          // 状态机：仅 pending 记录显示驳回/通过按钮（后端同样拒绝对已审核记录重复操作）；
          // approved/rejected 只给「关闭」。两个视图（本人/代审）共用同一组按钮
          detail?.status === 'pending'
            ? [
                <Button key="cancel" onClick={() => setDetailOpen(false)}>
                  取消
                </Button>,
                <Button
                  key="reject"
                  danger
                  icon={<CloseOutlined />}
                  loading={actionLoading}
                  onClick={handleReject}
                >
                  驳回
                </Button>,
                <Button
                  key="approve"
                  type="primary"
                  icon={<CheckOutlined />}
                  loading={actionLoading}
                  onClick={handleApprove}
                >
                  通过并入库
                </Button>,
              ]
            : [
                <Button key="close" onClick={() => setDetailOpen(false)}>
                  关闭
                </Button>,
              ]
        }
      >
        {detailLoading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>加载中…</div>
        ) : detail ? (
          <div>
            <Space direction="vertical" style={{ width: '100%' }} size="middle">
              <div>
                <Text strong>文件名：</Text>
                <Text>{detail.file_name}</Text>
                <Text type="secondary" style={{ marginLeft: 16 }}>
                  类型：{DOC_TYPE_MAP[detail.doc_type] || detail.doc_type}
                </Text>
              </div>

              {detail.reviewer_notes && (
                <div>
                  <Text strong>审核备注：</Text>
                  <Text>{detail.reviewer_notes}</Text>
                </div>
              )}

              <div>
                <Text strong>清洗后文本：</Text>
                {detail.status === 'pending' ? (
                  <TextArea
                    rows={15}
                    value={editedText}
                    onChange={(e) => setEditedText(e.target.value)}
                    placeholder="可在此编辑修正 OCR 识别结果…"
                    style={{ marginTop: 8, fontFamily: 'monospace', fontSize: 13 }}
                  />
                ) : (
                  <pre
                    style={{
                      marginTop: 8,
                      padding: 12,
                      background: '#f5f5f5',
                      borderRadius: 6,
                      maxHeight: 400,
                      overflow: 'auto',
                      fontSize: 13,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {detail.cleaned_text || '（无文本）'}
                  </pre>
                )}
              </div>

              {detail.status === 'pending' && editedText !== (detail.cleaned_text || '') && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  <EditOutlined /> 文本已修改，通过后将以编辑后的版本入库
                </Text>
              )}
            </Space>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
