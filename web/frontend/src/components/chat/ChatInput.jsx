/**
 * @文件 ChatInput.jsx
 * @作用 对话页底部输入区：Enter 发送 / Shift+Enter 换行；流式中可停止；
 * 上传按钮将文件作为【临时知识库】入库（绑定当前会话，会话结束自动删除）。
 * 上传过程显示真实字节进度，失败原因持久展示（不会像 toast 一样消失）。
 * @主要成员 ChatInput（默认导出，组件）；常量 MAX_INPUT_LEN（输入最大字符数 4000）、
 * UPLOAD_MAX_BYTES（单文件字节上限）、SUCCESS_AUTO_HIDE_MS（上传成功条 4 秒自动收起）；
 * 内部函数 doSend、pollUploadTask、customUpload、beforeUpload
 * @被谁使用 src/pages/ChatPage.jsx 引入，渲染在聊天页右下（消息流 MessageList 之下）；
 * 无 props，全部数据经 Zustand store 获取
 */
import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Input, Progress, Tooltip, Typography, Upload } from 'antd';
import { PaperClipOutlined, SendOutlined, StopOutlined } from '@ant-design/icons';
import { useChatStore } from '../../stores/chatStore.js';
import { useSessionStore } from '../../stores/sessionStore.js';
import { uploadFiles, getUploadStatus } from '../../api/fileApi.js';
import { UPLOAD_EXTENSIONS, UPLOAD_MAX_MB } from '../../api/contracts.js';

/** 输入框最大字符数（与后端 user_input 1-4000 限制对齐，前端提前拦截） */
const MAX_INPUT_LEN = 4000;
/** 上传单文件字节上限（UPLOAD_MAX_MB=50MB 换算），beforeUpload 前端拦截 */
const UPLOAD_MAX_BYTES = UPLOAD_MAX_MB * 1024 * 1024;
/** 上传成功 Alert 的自动收起延时（4 秒）；失败条不自动收起，需手动关闭 */
const SUCCESS_AUTO_HIDE_MS = 4000;

/**
 * 组件：ChatInput
 * 作用：聊天输入区组件——文本输入与发送/停止、附件上传（临时知识库）及上传进度/结果展示
 * 实例化/挂载位置：ChatPage 右侧栏底部（MessageList 之下）
 * 数据来源：useChatStore（src/stores/chatStore.js）的 streaming/send/abort（send 内消费
 * POST /chat/stream 的 SSE 流）；useSessionStore（src/stores/sessionStore.js）的 currentSessionId
 * 数据去向：发送消息走 chatStore.send（POST /chat/stream，SSE）；停止走 chatStore.abort
 * （abort 当前 SSE 请求）；上传走 uploadFiles（POST /file/path，scope='temp' 绑定当前会话）
 * 与 getUploadStatus（GET /file/status/{task_id} 轮询）。本组件不接收 props
 */
export default function ChatInput() {
  const { message } = App.useApp();
  // text：输入框文本，由输入 onChange 更新，doSend 发送后清空
  const [text, setText] = useState('');
  // up: null | { name, phase:'uploading'|'processing'|'polling'|'done', percent, status, failMsg }
  // up：当前单文件上传状态机（本组件一次仅允许一个上传），驱动进度条/成功条/失败条
  const [up, setUp] = useState(null);
  // hideTimerRef：成功条 4 秒自动收起定时器
  const hideTimerRef = useRef(null);
  // pollTimerRef：异步上传任务 2 秒轮询定时器（setTimeout 链式自调度）
  const pollTimerRef = useRef(null);
  // streaming：chatStore 流式生成中标志（SSE），为 true 时输入区显示「停止生成」按钮
  const streaming = useChatStore((s) => s.streaming);
  // send：chatStore 发送动作，内部发起 /chat/stream SSE 并写入消息列表
  const send = useChatStore((s) => s.send);
  // abort：chatStore 中止动作，abort 当前 SSE 的 AbortController（已收内容保留）
  const abort = useChatStore((s) => s.abort);
  // currentSessionId：当前会话 ID（sessionStore，持久化 sessionStorage）；temp 上传必须携带
  const currentSessionId = useSessionStore((s) => s.currentSessionId);

  // useEffect（仅挂载/卸载）：卸载时清理成功收起与轮询定时器
  useEffect(() => () => {
    clearTimeout(hideTimerRef.current);
    clearTimeout(pollTimerRef.current);
  }, []);

  /**
   * @function doSend
   * @description 发送文本：trim 校验非空与长度上限后交 chatStore 发送，并清空输入框
   * 被谁触发：「发送」按钮 onClick；TextArea 非 Shift 的 Enter（onPressEnter 内 preventDefault）
   * @returns {void} 无入参（读组件 state text 与 store.streaming）
   * @副作用 超长 message.warning；合法时调 chatStore.send（POST /chat/stream SSE）并 setText('')；
   * streaming 中或空文本直接忽略
   */
  function doSend() {
    const trimmed = text.trim();
    if (!trimmed || streaming) return;
    if (trimmed.length > MAX_INPUT_LEN) {
      message.warning(`输入不能超过 ${MAX_INPUT_LEN} 字`);
      return;
    }
    send(trimmed);
    setText('');
  }

  /**
   * @function pollUploadTask
   * @description 大文件异步上传的轮询器：每 2 秒查询后台处理状态，success/failed 收尾，否则继续自调度
   * 被谁触发：customUpload 收到 {status:'processing', task_id} 后首次调用；之后自身 setTimeout 链式触发
   * @param {string} taskId 后台任务 ID，拼入 GET /file/status/{taskId}
   * @param {string} fileName 文件名（仅用于成功/失败提示与状态展示）
   * @param {(result?:unknown)=>void} onSuccess antd customRequest 成功回调（结束 antd 上传态）
   * @param {(err:Error)=>void} onError antd customRequest 失败回调
   * @returns {Promise<void>} 无返回值
   * @副作用 调 getUploadStatus；setUp 更新进度/终态；成功 message.success 并安排 4 秒收起；异常标记失败
   */
  async function pollUploadTask(taskId, fileName, onSuccess, onError) {
    const POLL_INTERVAL_MS = 2000;
    try {
      const status = await getUploadStatus(taskId);
      if (status.status === 'success') {
        setUp({ name: fileName, phase: 'done', percent: 100, status: 'success', failMsg: '' });
        message.success(`「${fileName}」已作为临时知识库入库`);
        onSuccess?.();
        hideTimerRef.current = setTimeout(() => setUp(null), SUCCESS_AUTO_HIDE_MS);
        return;
      }
      if (status.status === 'failed') {
        const failMsg = status.message || '后台处理失败';
        setUp({ name: fileName, phase: 'done', percent: 100, status: 'fail', failMsg });
        onError?.(new Error(failMsg));
        return;
      }
      // 仍在处理中：更新进度后继续轮询
      setUp((u) => (u ? { ...u, phase: 'polling', percent: status.progress || 0 } : u));
      pollTimerRef.current = setTimeout(
        () => pollUploadTask(taskId, fileName, onSuccess, onError),
        POLL_INTERVAL_MS
      );
    } catch (e) {
      const failMsg = e.message || '查询上传进度失败';
      setUp((u) => ({
        name: u?.name || fileName,
        phase: 'done',
        percent: u?.percent || 0,
        status: 'fail',
        failMsg,
      }));
      onError?.(e);
    }
  }

  /**
   * @function customUpload
   * @description antd Upload 的自定义上传实现：把所选单文件以临时知识库（scope='temp'）
   * 绑定当前会话上传；含无会话拦截、真实字节进度、异步大文件轮询与成败收尾
   * 被谁触发：beforeUpload 返回 undefined 后由 antd 调用（替代其内置 XHR 上传）
   * @param {{file:File, onSuccess?:(result?:unknown)=>void, onError?:(err:Error)=>void}} options
   * antd 注入：file 为浏览器 File（用户选择，必填）；onSuccess/onError 为 antd 上传状态回调
   * @returns {Promise<void>} 无返回值
   * @副作用 无会话时持久失败提示并 onError；否则调 POST /file/path（uploadFiles）；
   * processing 转 pollUploadTask；setUp 全流程更新；AbortError 静默
   */
  async function customUpload({ file, onSuccess, onError }) {
    if (!currentSessionId) {
      // 无会话时持久展示失败原因（toast 容易被忽略）
      setUp({ name: file.name, phase: 'done', percent: 0, status: 'fail', failMsg: '请先发送一条消息创建会话，再上传文件' });
      onError?.(new Error('no session'));
      return;
    }
    clearTimeout(hideTimerRef.current);
    clearTimeout(pollTimerRef.current);
    setUp({ name: file.name, phase: 'uploading', percent: 0, status: 'uploading', failMsg: '' });
    try {
      const result = await uploadFiles(
        [file],
        { scope: 'temp', sessionId: currentSessionId },
        (p) =>
          setUp((u) =>
            u
              ? { ...u, percent: p.percent, phase: p.percent >= 100 ? 'processing' : 'uploading' }
              : u
          )
      );

      // 大文件走异步后台处理：响应为 { status:'processing', task_id }
      if (result.status === 'processing' && result.task_id) {
        setUp((u) => (u ? { ...u, phase: 'polling', percent: 0 } : u));
        pollUploadTask(result.task_id, file.name, onSuccess, onError);
        return;
      }

      const ok = result.files.find((f) => f.status === 'success');
      if (ok) {
        setUp({ name: file.name, phase: 'done', percent: 100, status: 'success', failMsg: '' });
        message.success(`「${file.name}」已作为临时知识库入库`);
        onSuccess?.(result);
        // 成功条 4 秒后自动收起；失败条保留直到手动关闭
        hideTimerRef.current = setTimeout(() => setUp(null), SUCCESS_AUTO_HIDE_MS);
      } else {
        const fail = result.files[0];
        const failMsg = fail?.message || '上传失败';
        setUp({ name: file.name, phase: 'done', percent: 100, status: 'fail', failMsg });
        onError?.(new Error(failMsg));
      }
    } catch (e) {
      if (e?.name === 'AbortError') return;
      const failMsg = e.message || '上传失败';
      setUp((u) => ({
        name: u?.name || file.name,
        phase: 'done',
        percent: u?.percent || 0,
        status: 'fail',
        failMsg,
      }));
      onError?.(e);
    }
  }

  /**
   * @function beforeUpload
   * @description antd Upload 选择前钩子：校验扩展名与单文件大小，非法则提示并剔除
   * 被谁触发：选择附件后由 antd 在 customRequest 之前回调
   * @param {File} file 当前选中的浏览器 File 对象（含 name/size）
   * @returns {symbol|undefined} 非法返回 Upload.LIST_IGNORE（阻止上传）；
   * 合法返回 undefined——允许 antd 继续走 customRequest（返回 false 会同时禁止 customRequest）
   * @副作用 校验不通过时 message.error 提示
   */
  function beforeUpload(file) {
    const ok = UPLOAD_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext));
    if (!ok) {
      message.error(`仅支持 ${UPLOAD_EXTENSIONS.join(' / ')} 格式文件`);
      return Upload.LIST_IGNORE;
    }
    if (file.size > UPLOAD_MAX_BYTES) {
      message.error(`文件超过 ${UPLOAD_MAX_MB}MB 上限`);
      return Upload.LIST_IGNORE;
    }
    // 返回 undefined：允许 antd 继续走 customRequest（返回 false 会同时禁止 customRequest）
    return undefined;
  }

  // uploading：派生值——是否处于字节上传阶段（up.status==='uploading'），用于禁用上传入口与按钮 loading
  const uploading = up?.status === 'uploading';

  return (
    <div style={{ borderTop: '1px solid #ececec', padding: '12px 16px', flexShrink: 0 }}>
      {up && up.status === 'uploading' && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            marginBottom: 8,
            padding: '6px 10px',
            background: '#f6ffed',
            border: '1px solid #b7eb8f',
            borderRadius: 6,
          }}
        >
          <Typography.Text ellipsis style={{ maxWidth: 220 }} title={up.name}>
            {up.name}
          </Typography.Text>
          <Progress
            size="small"
            percent={up.percent}
            status={up.phase === 'processing' || up.phase === 'polling' ? 'active' : 'normal'}
            style={{ flex: 1, marginBottom: 0, minWidth: 120 }}
          />
          <Typography.Text type="secondary" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
            {up.phase === 'processing'
              ? '解析入库中…'
              : up.phase === 'polling'
                ? `后台处理中 ${up.percent}%`
                : `${up.percent}%`}
          </Typography.Text>
        </div>
      )}

      {up && up.status === 'success' && (
        <Alert
          type="success"
          showIcon
          banner
          style={{ marginBottom: 8 }}
          message={`「${up.name}」上传成功，已可在当前会话中检索`}
        />
      )}

      {up && up.status === 'fail' && (
        <Alert
          type="error"
          showIcon
          closable
          banner
          style={{ marginBottom: 8 }}
          onClose={() => setUp(null)}
          message={`「${up.name}」上传失败`}
          description={up.failMsg}
        />
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        <Upload
          accept={UPLOAD_EXTENSIONS.join(',')}
          showUploadList={false}
          disabled={uploading}
          beforeUpload={beforeUpload}
          customRequest={customUpload}
        >
          <Tooltip title={uploading ? '上传中…' : `上传文件到知识库（pdf/txt/md，≤${UPLOAD_MAX_MB}MB）`}>
            <Button icon={<PaperClipOutlined />} loading={uploading} aria-label="上传文件" />
          </Tooltip>
        </Upload>

        <Input.TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              doSend();
            }
          }}
          placeholder="输入你的问题，Enter 发送，Shift+Enter 换行"
          autoSize={{ minRows: 1, maxRows: 6 }}
          maxLength={MAX_INPUT_LEN}
          style={{ flex: 1 }}
        />

        {streaming ? (
          <Tooltip title="停止生成">
            <Button danger icon={<StopOutlined />} onClick={abort} aria-label="停止生成" />
          </Tooltip>
        ) : (
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={doSend}
            disabled={!text.trim()}
            aria-label="发送"
          >
            发送
          </Button>
        )}
      </div>
    </div>
  );
}
