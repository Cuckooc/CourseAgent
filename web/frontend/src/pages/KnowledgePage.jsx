/**
 * @文件 KnowledgePage.jsx
 * @作用 知识库管理页（长期知识库）：
 * - 上传单文件或文件夹（多文件），可随时追加 / 删除，即时更新向量库
 * - 文件列表与上方「上传范围」联动：仅展示当前所选知识库（公共/私有）的文件
 * - 文件名展示用户上传时的原始文件名（含扩展名），删除仍按存储名执行
 * - 删除二次确认（向量分块与物理文件同删）
 *
 * 知识库范围：
 * - public：公共知识库（基础知识库），所有用户都可 RAG，teacher/admin 可上传，admin 在此查看全局公共文件
 * - private：用户个人私有知识库（teacher/user），长期保存，仅所有者可见，可随时更新
 * - temp（临时知识库）不在此页管理：仅在对话中上传，绑定当前会话，
 *   会话结束即从内存与临时目录清除，不写入本地向量数据库
 * @主要成员 KnowledgePage（默认导出，页面组件）；常量 UPLOAD_MAX_BYTES（单文件字节上限）、
 * SUCCESS_AUTO_HIDE_MS（上传成功提示自动收起毫秒数）；内部函数 refresh、beforeUpload、
 * pollUploadTask、startBatchUpload、confirmDelete，以及表格列配置 columns
 * @被谁使用 src/router/index.jsx 以 React.lazy 懒加载挂载于路由 /knowledge；
 * 允许角色 user/teacher/admin（MAIN_NAV），外层 RequireAuth + RequirePermission 守卫；
 * 渲染在 AppLayout 的 <Outlet/> 中；侧栏「知识库」菜单导航到此
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Progress,
  Radio,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd';
import {
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  InboxOutlined,
} from '@ant-design/icons';
import { deleteKnowledgeFile, fetchKnowledgeList } from '../api/knowledgeApi.js';
import { uploadFiles, getUploadStatus } from '../api/fileApi.js';
import { UPLOAD_EXTENSIONS, UPLOAD_MAX_MB } from '../api/contracts.js';
import { useAuthStore } from '../stores/authStore.js';

/** 单文件字节上限：由契约常量 UPLOAD_MAX_MB（50MB）换算，beforeUpload 前端拦截用 */
const UPLOAD_MAX_BYTES = UPLOAD_MAX_MB * 1024 * 1024;
/** 整批上传成功后结果 Alert 的自动收起延时（8 秒）；失败/部分失败不自动收起 */
const SUCCESS_AUTO_HIDE_MS = 8000;

/**
 * 组件：KnowledgePage
 * 作用：长期知识库管理页：范围切换（公共/私有）、文件/文件夹拖拽上传（含异步轮询与进度）、
 * 文件表格展示与二次确认删除
 * 实例化/挂载位置：路由 /knowledge，经 AppLayout 的 <Outlet/> 渲染
 * 数据来源：useAuthStore（src/stores/authStore.js）的 role（决定范围可选项与上传入口显隐）；
 * fetchKnowledgeList（GET /knowledge/list）；uploadFiles（POST /file/path，XHR 真实进度）；
 * getUploadStatus（GET /file/status/{task_id}）；deleteKnowledgeFile（/knowledge/delete 两步确认）
 * 数据去向：上传提交到 POST /file/path（multipart，scope=public/private）；
 * 删除经 preview → confirm 两步 POST；成功后均 refresh() 重新拉列表
 */
export default function KnowledgePage() {
  const { message, modal } = App.useApp();
  // role：当前角色（authStore，源自登录响应并由 /login/me 同步）；admin 仅看公共库且禁传
  const role = useAuthStore((s) => s.role);
  const isAdmin = role === 'admin';
  const isTeacher = role === 'teacher';

  // rows：知识库文件全量行（接口一次返回），含 {filename, original_name, scope, orphan,...}
  const [rows, setRows] = useState([]);
  // loading：列表加载中，驱动 Table 的 loading；由 refresh 切换
  const [loading, setLoading] = useState(false);
  // 上传批次状态：
  // - phase=uploading：字节传输中，percent 为真实进度
  // - phase=processing：传输完成，服务端解析/向量化中（等待响应）
  // - phase=polling：大上传走后台异步处理，轮询任务进度（percent 为后端整体进度）
  // - phase=done：本批结束，resultType=success/partial/error，失败明细持久展示
  const [batch, setBatch] = useState(null);
  // beforeUpload 聚合同一次选择/拖入的多个文件（含文件夹），合并为单个 multipart 请求
  const pickedRef = useRef([]);
  // flushTimerRef：50ms 聚合定时器，收齐同批文件后统一发起一次上传
  const flushTimerRef = useRef(null);
  // hideTimerRef：成功结果 8 秒自动收起定时器
  const hideTimerRef = useRef(null);
  // pollTimerRef：异步上传任务 2 秒轮询定时器（setTimeout 链式自调度）
  const pollTimerRef = useRef(null);

  // useEffect（仅挂载/卸载）：卸载时清理全部定时器，避免卸载后 setState 与无效轮询
  useEffect(() => () => {
    clearTimeout(flushTimerRef.current);
    clearTimeout(hideTimerRef.current);
    clearTimeout(pollTimerRef.current);
  }, []);
  // 上传范围：teacher 可选公共/私有；admin 仅查看公共库（不可上传）；普通用户仅私有
  const [scope, setScope] = useState(isAdmin ? 'public' : 'private');

  /**
   * @function refresh
   * @description 拉取知识库文件列表并排序（孤儿文件置顶，其余按文件名排序）
   * 被谁触发：挂载 effect；上传/删除成功后调用以刷新表格
   * @returns {Promise<void>} 无返回值
   * @副作用 调 GET /knowledge/list（fetchKnowledgeList）；setRows、setLoading；失败静默留空
   */
  async function refresh() {
    setLoading(true);
    try {
      const res = await fetchKnowledgeList();
      const sorted = [...res.data].sort(
        (a, b) => Number(a.orphan) - Number(b.orphan) || a.filename.localeCompare(b.filename)
      );
      setRows(sorted);
    } catch {
      // 初始化加载失败：静默，列表保持空白，不打扰用户
    } finally {
      setLoading(false);
    }
  }

  // useEffect（依赖 []，仅挂载一次）：首屏拉取知识库文件列表
  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // useMemo（依赖 [rows, scope]）渲染辅助：
  // 文件列表与「上传范围」联动：仅展示当前所选知识库的文件。
  // 孤儿文件无法归类（scope=unknown），在两个范围下都显示以便清理。
  const visibleRows = useMemo(
    () => rows.filter((r) => r.orphan || r.scope === scope),
    [rows, scope]
  );

  /**
   * @function beforeUpload
   * @description antd Upload 的选择前钩子：校验扩展名/大小，非法文件当场提示并剔除；
   * 合法文件聚合到 pickedRef，并在首批文件到达时启动 50ms 定时器统一发起一次请求。
   * antd 对每个选中文件都会回调 beforeUpload(file, fileList)：
   * - 返回 false / LIST_IGNORE 都会阻止 antd 发起请求（customRequest 也不会触发），
   *   因此这里把同一次选择/拖入的文件聚合到 pickedRef，由定时器统一发起「一个」请求；
   * - 扩展名/大小不合法的文件当场提示并剔除，不进入批次。
   * 被谁触发：Upload.Dragger 选择/拖入文件时由 antd 逐文件回调
   * @param {File} file 当前被钩子拦截的浏览器 File 对象（含 name/size）
   * @returns {symbol} 恒返回 Upload.LIST_IGNORE（阻止 antd 自动上传，改走自定义聚合请求）
   * @副作用 可能 message.error 提示；写 pickedRef；启动 flushTimer 调 startBatchUpload
   */
  function beforeUpload(file) {
    const lowerName = file.name.toLowerCase();
    const extOk = UPLOAD_EXTENSIONS.some((ext) => lowerName.endsWith(ext));
    if (!extOk) {
      message.error(`「${file.name}」格式不支持，仅支持 ${UPLOAD_EXTENSIONS.join(' / ')}`);
      return Upload.LIST_IGNORE;
    }
    if (file.size > UPLOAD_MAX_BYTES) {
      message.error(`「${file.name}」超过单文件 ${UPLOAD_MAX_MB}MB 上限，已跳过`);
      return Upload.LIST_IGNORE;
    }
    if (pickedRef.current.length === 0) {
      clearTimeout(flushTimerRef.current);
      // 同一次选择的 beforeUpload 回调在同一轮任务内连续发生，50ms 足够收齐
      flushTimerRef.current = setTimeout(() => {
        const files = pickedRef.current;
        pickedRef.current = [];
        startBatchUpload(files);
      }, 50);
    }
    pickedRef.current.push(file);
    return Upload.LIST_IGNORE;
  }

  /**
   * @function pollUploadTask
   * @description 异步上传任务轮询器：每 2 秒查一次后端处理进度，直到 success/failed；
   * processing 期间更新 batch.percent 继续自调度
   * 被谁触发：startBatchUpload 收到 {status:'processing', task_id} 响应后首次调用；
   * 之后由自身 setTimeout 链式触发（SSE 无关，HTTP 轮询）
   * @param {string} taskId 上传接口返回的后台任务 ID，拼入 GET /file/status/{task_id}
   * @param {Array<{name:string,status:string,message:string}>} items 本批文件结果项（与提交顺序一致）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 getUploadStatus（GET /file/status/{taskId}）；setBatch 更新进度/终态；
   * 结束后 refresh() 刷新列表，成功安排 8 秒收起定时器；异常整批标记失败
   */
  async function pollUploadTask(taskId, items) {
    // 轮询异步上传任务进度，直到 success/failed 或网络错误
    const POLL_INTERVAL_MS = 2000;
    try {
      const status = await getUploadStatus(taskId);
      if (status.status === 'success' || status.status === 'failed') {
        // 后台处理结束：成功统一标记 success（后端任一文件失败即整体 failed）
        const allSuccess = status.status === 'success';
        const doneItems = items.map((i) => ({
          ...i,
          status: allSuccess ? 'success' : 'fail',
          message: allSuccess ? '' : status.message || '后台处理失败',
        }));
        const resultType = allSuccess ? 'success' : 'error';
        setBatch({ phase: 'done', percent: 100, items: doneItems, resultType });
        await refresh();
        if (resultType === 'success') {
          hideTimerRef.current = setTimeout(() => setBatch(null), SUCCESS_AUTO_HIDE_MS);
        }
        return;
      }
      // 仍在处理中：更新整体进度百分比后继续轮询
      setBatch((b) =>
        b ? { ...b, phase: 'polling', percent: status.progress || 0 } : b
      );
      pollTimerRef.current = setTimeout(() => pollUploadTask(taskId, items), POLL_INTERVAL_MS);
    } catch (e) {
      // 轮询接口异常：标记整批失败
      setBatch((b) => ({
        phase: 'done',
        percent: b?.percent || 0,
        resultType: 'error',
        error: e.message || '查询上传进度失败',
        items: (b?.items || items).map((i) => ({
          ...i,
          status: 'fail',
          message: i.message || e.message || '查询上传进度失败',
        })),
      }));
    }
  }

  /**
   * @function startBatchUpload
   * @description 批次上传主流程：初始化批次状态 → XHR 上传（真实字节进度回调）→
   * 大上传转轮询 / 同步结果按索引对齐每个文件成败 → 刷新列表并安排成功提示收起
   * 被谁触发：beforeUpload 聚合定时器（flushTimer）50ms 收齐同批文件后调用
   * @param {File[]} files 同一次选择/拖入聚合的文件数组（至少 1 个），来自 pickedRef
   * @returns {Promise<void>} 无返回值
   * @副作用 调 uploadFiles（POST /file/path，multipart，携带当前 scope）；
   * 异步则调 pollUploadTask；全程 setBatch；AbortError 静默，其他失败整批标记
   */
  async function startBatchUpload(files) {
    if (!files.length) return;
    clearTimeout(hideTimerRef.current);
    clearTimeout(pollTimerRef.current);
    const items = files.map((f) => ({ name: f.name, status: 'uploading', message: '' }));
    setBatch({ phase: 'uploading', percent: 0, items, resultType: null });

    try {
      const result = await uploadFiles(files, { scope }, (p) => {
        setBatch((b) =>
          b
            ? { ...b, percent: p.percent, phase: p.percent >= 100 ? 'processing' : 'uploading' }
            : b
        );
      });

      // 大上传走异步后台处理：响应为 { status:'processing', task_id }
      if (result.status === 'processing' && result.task_id) {
        setBatch((b) => (b ? { ...b, phase: 'polling', percent: 0 } : b));
        pollUploadTask(result.task_id, items);
        return;
      }

      // 同步上传：后端 results 与 multipart 文件顺序一致，按索引对齐
      const doneItems = items.map((item, idx) => {
        const r = result.files[idx] || {};
        return r.status === 'success'
          ? { ...item, status: 'success' }
          : { ...item, status: 'fail', message: r.message || '处理失败' };
      });
      const failCount = doneItems.filter((i) => i.status === 'fail').length;
      const resultType = failCount === 0 ? 'success' : failCount === doneItems.length ? 'error' : 'partial';
      setBatch({ phase: 'done', percent: 100, items: doneItems, resultType });
      await refresh();
      if (resultType === 'success') {
        // 成功提示 8 秒后自动收起；失败/部分失败保留直到用户手动关闭
        hideTimerRef.current = setTimeout(() => setBatch(null), SUCCESS_AUTO_HIDE_MS);
      }
    } catch (e) {
      if (e?.name === 'AbortError') return;
      // 请求级失败（401/403/413/429/500/网络/超时）：整批标记失败并展示后端原因
      setBatch((b) => ({
        phase: 'done',
        percent: b?.percent || 0,
        resultType: 'error',
        error: e.message || '上传失败',
        items: (b?.items || items).map((i) => ({
          ...i,
          status: 'fail',
          message: i.message || e.message || '上传失败',
        })),
      }));
    }
  }

  /**
   * @function confirmDelete
   * @description 点击行内「删除」：弹出二次确认框；确认后执行两步删除并刷新列表
   * 被谁触发：文件表格操作列「删除」按钮 onClick（render 内绑定，参数为当前行）
   * @param {{filename:string, original_name?:string, scope:string, orphan?:boolean}} row
   * 行数据（来自 visibleRows）；删除按存储名 filename 执行
   * @returns {void} 弹窗本身无返回值
   * @副作用 onOk 内调 deleteKnowledgeFile（POST /knowledge/delete/preview + /confirm）；
   * 成功 message.success 并 refresh()；失败 message.error
   */
  function confirmDelete(row) {
    modal.confirm({
      title: '确认删除该文件？',
      content: `将同时删除其向量分块与物理文件：${row.original_name || row.filename}`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          const res = await deleteKnowledgeFile(row.filename);
          message.success(`删除成功：清除 ${res.deleted_chunks} 个分块${res.file_removed ? '，物理文件已移除' : ''}`);
          await refresh();
        } catch (e) {
          message.error(e.message || '删除失败');
        }
      },
    });
  }

  /**
   * columns：antd Table 列配置（渲染辅助）。
   * - 文件名列：原始文件名 + 存储名 Tooltip；状态列：orphan 显示「禁用」否则「正常」；
   * - 操作列：删除按钮，点击调 confirmDelete(row)
   */
  const columns = [
    {
      title: '文件名',
      dataIndex: 'original_name',
      render: (name, row) => (
        <Space>
          <FileTextOutlined style={{ color: '#999' }} />
          <Tooltip title={`存储名：${row.filename}`}>
            <Typography.Text copyable={{ text: name }}>{name}</Typography.Text>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'orphan',
      width: 100,
      render: (orphan) =>
        orphan ? (
          <Tag color="default">禁用</Tag>
        ) : (
          <Tag color="success">正常</Tag>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      render: (_, row) => (
        <Button size="small" danger icon={<DeleteOutlined />} onClick={() => confirmDelete(row)}>
          删除
        </Button>
      ),
    },
  ];

  return (
    <div style={{ padding: 16, maxWidth: 1100, margin: '0 auto' }}>
      <Card title="知识库管理" styles={{ body: { paddingTop: 12 } }}>
        <Space style={{ marginBottom: 12 }} wrap>
          <span>知识库范围：</span>
          {isAdmin ? (
            <Radio.Group value="public" disabled>
              <Radio.Button value="public">公共知识库</Radio.Button>
            </Radio.Group>
          ) : isTeacher ? (
            <Radio.Group value={scope} onChange={(e) => setScope(e.target.value)}>
              <Radio.Button value="public">公共知识库</Radio.Button>
              <Radio.Button value="private">我的私有知识库</Radio.Button>
            </Radio.Group>
          ) : (
            <Radio.Group value="private" disabled>
              <Radio.Button value="private">我的私有知识库</Radio.Button>
            </Radio.Group>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {scope === 'public'
              ? isAdmin
                ? '公共知识库（管理员查看）：全部公共文件信息，教师可上传维护'
                : '公共知识库（基础知识库）：所有用户问答时均可检索使用'
              : '私有知识库：仅您本人可检索，可随时追加或删除文件'}
          </Typography.Text>
        </Space>

        {!isAdmin && (
          <Upload.Dragger
            accept={UPLOAD_EXTENSIONS.join(',')}
            multiple
            showUploadList={false}
            disabled={batch && batch.phase !== 'done'}
            beforeUpload={beforeUpload}
            style={{ marginBottom: 16, background: '#fafafa' }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">
              {batch?.phase === 'uploading'
                ? `正在上传 ${batch.items.length} 个文件（${batch.percent}%）…`
                : batch?.phase === 'processing'
                  ? '文件已上传，正在解析并写入向量库…'
                  : batch?.phase === 'polling'
                    ? `大文件后台处理中（${batch.percent}%）…`
                    : '点击选择文件，或拖拽文件 / 文件夹到此处上传'}
            </p>
            <p className="ant-upload-hint">
              支持 {UPLOAD_EXTENSIONS.join(' / ')}，单文件不超过 {UPLOAD_MAX_MB}MB，
              可多选单个文件；如需上传整个文件夹（自动遍历其中文件）请点下方按钮
            </p>
            {/*
              文件夹选择入口：外层 Dragger 已去掉 directory（点击主体走普通文件
              选择框）；此处内嵌一个 directory Upload 专门选文件夹。独立 div 包裹
              （antd Upload 外层渲染为 div，不能放进上方 <p> 内，否则 DOM 嵌套非法），
              并截停点击冒泡，避免同时触发外层 Dragger 的文件选择框。
            */}
            <div
              onClick={(e) => e.stopPropagation()}
              style={{ textAlign: 'center' }}
            >
              <Upload
                directory
                multiple
                showUploadList={false}
                disabled={batch && batch.phase !== 'done'}
                beforeUpload={beforeUpload}
              >
                <Button
                  size="small"
                  icon={<FolderOpenOutlined />}
                  disabled={batch && batch.phase !== 'done'}
                >
                  选择文件夹
                </Button>
              </Upload>
            </div>
          </Upload.Dragger>
        )}

        {batch && batch.phase !== 'done' && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            banner
            message={
              batch.phase === 'uploading'
                ? `正在上传 ${batch.items.length} 个文件（${batch.percent}%）`
                : batch.phase === 'polling'
                  ? `后台处理中（${batch.percent}%），${batch.items.length} 个文件`
                  : `文件已全部发送，正在解析并写入向量库，请稍候…（${batch.items.length} 个文件）`
            }
            description={
              <Progress
                percent={batch.percent}
                status={batch.phase === 'processing' || batch.phase === 'polling' ? 'active' : 'normal'}
              />
            }
          />
        )}

        {batch && batch.phase === 'done' && (
          <Alert
            type={batch.resultType === 'success' ? 'success' : batch.resultType === 'partial' ? 'warning' : 'error'}
            showIcon
            closable
            banner
            style={{ marginBottom: 16 }}
            onClose={() => setBatch(null)}
            message={
              batch.resultType === 'success'
                ? `${batch.items.length} 个文件全部成功入库`
                : `${batch.items.filter((i) => i.status === 'success').length} 个成功，${batch.items.filter((i) => i.status === 'fail').length
                } 个失败${batch.error ? `：${batch.error}` : ''}`
            }
            description={
              batch.items.some((i) => i.status === 'fail') ? (
                <ul style={{ margin: 0, paddingLeft: 18, maxHeight: 200, overflow: 'auto' }}>
                  {batch.items
                    .filter((i) => i.status === 'fail')
                    .map((i, idx) => (
                      <li key={`fail-${idx}-${i.name}`} style={{ marginBottom: 4 }}>
                        <CloseCircleFilled style={{ color: '#ff4d4f', marginRight: 6 }} />
                        <Typography.Text strong>{i.name}</Typography.Text>
                        <Typography.Text type="secondary">：{i.message}</Typography.Text>
                      </li>
                    ))}
                </ul>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18, maxHeight: 200, overflow: 'auto' }}>
                  {batch.items.map((i, idx) => (
                    <li key={`ok-${idx}-${i.name}`} style={{ marginBottom: 4 }}>
                      <CheckCircleFilled style={{ color: '#52c41a', marginRight: 6 }} />
                      {i.name}
                    </li>
                  ))}
                </ul>
              )
            }
          />
        )}

        <Table
          rowKey="filename"
          size="middle"
          loading={loading}
          dataSource={visibleRows}
          columns={columns}
          pagination={false}
          locale={{
            emptyText:
              scope === 'public'
                ? '公共知识库暂无文件，上传后在此管理'
                : '我的私有知识库暂无文件，上传后在此管理',
          }}
        />
      </Card>
    </div>
  );
}
