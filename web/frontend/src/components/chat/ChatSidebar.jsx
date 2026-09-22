/**
 * @文件 ChatSidebar.jsx
 * @作用 会话列表侧栏：点击切换、悬浮出重命名/删除入口、内联编辑。
 * 删除会话时后端会同时清理该会话的临时知识库。
 * @主要成员 ChatSidebar（默认导出，组件）；常量 MAX_TITLE_LEN（会话标题上限 100 字）、
 * UNDO_MESSAGE_KEY（删除撤销提示条 key）；
 * 内部函数 startEdit、cancelEdit、saveEdit、handleDelete、handleUndo
 * @被谁使用 src/pages/ChatPage.jsx 引入，渲染在聊天页最左侧；
 * 无 props，会话数据与操作全部经 useSessionStore（src/stores/sessionStore.js）
 */
import { useState } from 'react';
import { App, Button, Input, Spin, Tooltip, Typography, Popconfirm } from 'antd';
import { DeleteOutlined, EditOutlined } from '@ant-design/icons';
import { useSessionStore } from '../../stores/sessionStore.js';

/** 内联重命名标题最大字符数（前端校验，与后端标题长度限制对齐） */
const MAX_TITLE_LEN = 100;

/**
 * 删除成功提示条的固定 message key：连续删除多条会话时新提示覆盖旧提示，
 * 与后端 UndoStore「每用户只保留最近一次删除快照」的一次性撤销语义一致。
 */
const UNDO_MESSAGE_KEY = 'session-deleted-undo';

/**
 * 组件：ChatSidebar
 * 作用：会话列表侧栏——展示全部会话、点击切换当前会话、内联重命名、二次确认删除
 * 实例化/挂载位置：ChatPage 最左侧（宽 240px 固定栏）
 * 数据来源：useSessionStore（Zustand，src/stores/sessionStore.js）的
 * sessions（列表，POST /history/list 由 ChatPage 挂载拉取）、currentSessionId、loading
 * 及 select/rename/remove 动作
 * 数据去向：select 写 currentSessionId（持久化 sessionStorage，ChatPage 监听后加载
 * POST /history/detail）；rename → POST /history/update_title；
 * remove → POST /history/delete/preview + /confirm（后端连带清理临时知识库）
 */
export default function ChatSidebar() {
  const { message } = App.useApp();
  // sessions：会话列表（sessionStore，由 ChatPage 挂载时 fetchList 填充）
  const sessions = useSessionStore((s) => s.sessions);
  // currentSessionId：当前会话 ID，用于高亮当前行
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  // loading：列表加载中，控制首屏 Spin
  const loading = useSessionStore((s) => s.loading);
  // select：切换当前会话动作（写 store + sessionStorage；聊天记录由 ChatPage effect 加载）
  const select = useSessionStore((s) => s.select);
  // rename：重命名动作（POST /history/update_title，成功同步本地列表）
  const rename = useSessionStore((s) => s.rename);
  // remove：删除会话动作（两步确认 POST，删当前会话会清空当前指向）
  const remove = useSessionStore((s) => s.remove);
  // undoRemove：撤销最近一次删除动作（POST /history/undo_delete，成功后自动刷新列表）
  const undoRemove = useSessionStore((s) => s.undoRemove);

  // editingId：正在内联编辑的会话 ID；null 表示无编辑行（仅一行可处于编辑态）
  const [editingId, setEditingId] = useState(null);
  // draft：内联编辑中的标题草稿，随输入更新，保存时 trim 提交
  const [draft, setDraft] = useState('');
  // saving：重命名提交中，禁用输入框防止重复提交；由 saveEdit 切换
  const [saving, setSaving] = useState(false);

  /**
   * @function startEdit
   * @description 进入某行的内联编辑态并用原标题初始化草稿
   * 被谁触发：行内「重命名」铅笔图标 onClick（已 stopPropagation，不触发切换会话）
   * @param {{session_id:number, title:string}} item 当前会话行数据（来自 sessions）
   * @returns {void} 副作用为 setEditingId/setDraft
   */
  function startEdit(item) {
    setEditingId(item.session_id);
    setDraft(item.title);
  }

  /**
   * @function cancelEdit
   * @description 退出内联编辑态并清空草稿（放弃修改）
   * 被谁触发：编辑输入框按 Escape 键（onKeyDown）
   * @returns {void} 副作用为 setEditingId(null)/setDraft('')
   */
  function cancelEdit() {
    setEditingId(null);
    setDraft('');
  }

  /**
   * @function saveEdit
   * @description 保存内联重命名：校验非空与长度后提交 store，成功退出编辑态
   * 被谁触发：编辑输入框回车（onPressEnter）与输入框失焦（onBlur）
   * @param {number} sessionId 被编辑的会话 ID（来自当前行 item.session_id）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 sessionStore.rename（POST /history/update_title）；
   * 空/超长 message.warning 且不退出编辑；失败 message.error；setSaving 控制输入禁用
   */
  async function saveEdit(sessionId) {
    const title = draft.trim();
    if (!title) {
      message.warning('标题不能为空');
      return;
    }
    if (title.length > MAX_TITLE_LEN) {
      message.warning(`标题不能超过 ${MAX_TITLE_LEN} 字`);
      return;
    }
    setSaving(true);
    try {
      await rename(sessionId, title);
      setEditingId(null);
    } catch (e) {
      message.error(e.message || '重命名失败');
    } finally {
      setSaving(false);
    }
  }

  /**
   * @function handleDelete
   * @description 删除会话（Popconfirm 确认后的执行体）：调 store 删除，成功后弹出
   *   带「撤销」按钮的提示条（15 秒内可撤销最近一次删除）
   * 被谁触发：行内删除图标的 Popconfirm onConfirm（用户在气泡中点「删除」）
   * @param {{session_id:number, title:string}} item 当前会话行数据（来自 sessions）
   * @returns {Promise<void>} 无返回值
   * @副作用 调 sessionStore.remove（POST /history/delete/preview + /confirm，
   * 后端同时清理该会话临时知识库）；成功 message.open 挂撤销按钮，失败 message.error
   */
  async function handleDelete(item) {
    try {
      await remove(item.session_id);
      // 注意：antd v5 的 message.open 已移除 v4 的顶层 btn 参数（传入会被静默忽略，
      // 按钮不渲染）；v5 官方做法是把操作按钮放进 content ReactNode 内
      message.open({
        key: UNDO_MESSAGE_KEY,
        type: 'success',
        content: (
          <span>
            会话已删除
            <Button size="small" type="link" onClick={handleUndo} style={{ paddingInline: 8 }}>
              撤销
            </Button>
          </span>
        ),
        duration: 15,
      });
    } catch (e) {
      message.error(e.message || '删除失败');
    }
  }

  /**
   * @function handleUndo
   * @description 撤销删除：关闭提示条 → 调 store.undoRemove（POST /history/undo_delete）→
   *   列表自动刷新；按后端返回的恢复类型给成功文案
   * 被谁触发：删除成功提示条上的「撤销」按钮 onClick
   * @returns {Promise<void>} 无返回值
   * @副作用 成功 message.success（会话/画像文案不同）；404（快照已过期或不存在）/500
   *   时 message.error 展示后端原因
   */
  async function handleUndo() {
    message.destroy(UNDO_MESSAGE_KEY);
    try {
      const recoveredType = await undoRemove();
      message.success(recoveredType === 'profile' ? '已恢复用户画像' : '会话已恢复');
    } catch (e) {
      message.error(e.message || '恢复失败，删除记录可能已过期');
    }
  }

  return (
    <div
      style={{
        width: 240,
        flexShrink: 0,
        borderRight: '1px solid #ececec',
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        background: '#fafafa',
      }}
    >
      <div style={{ padding: '12px 16px 8px', fontWeight: 600, color: '#555' }}>会话列表</div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '0 8px 8px' }}>
        {loading && !sessions.length ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : !sessions.length ? (
          <Typography.Text type="secondary" style={{ display: 'block', padding: '16px 8px' }}>
            暂无会话，点击左侧「新对话」或直接发送消息开始咨询
          </Typography.Text>
        ) : (
          sessions.map((item) => {
            const active = item.session_id === currentSessionId;
            const editing = editingId === item.session_id;
            return (
              <div
                key={item.session_id}
                onClick={() => !editing && select(item.session_id)}
                style={{
                  padding: '8px 10px',
                  borderRadius: 8,
                  cursor: editing ? 'default' : 'pointer',
                  marginBottom: 2,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  background: active ? '#e6f4ff' : 'transparent',
                  color: active ? '#1677ff' : 'inherit',
                }}
                onMouseEnter={(e) => {
                  if (!active && !editing) e.currentTarget.style.background = '#f0f0f0';
                }}
                onMouseLeave={(e) => {
                  if (!active && !editing) e.currentTarget.style.background = 'transparent';
                }}
              >
                {editing ? (
                  <Input
                    size="small"
                    value={draft}
                    autoFocus
                    maxLength={MAX_TITLE_LEN}
                    disabled={saving}
                    onChange={(e) => setDraft(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onPressEnter={() => saveEdit(item.session_id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') cancelEdit();
                    }}
                    onBlur={() => saveEdit(item.session_id)}
                    style={{ flex: 1 }}
                  />
                ) : (
                  <>
                    <span
                      style={{
                        flex: 1,
                        minWidth: 0,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      title={item.title}
                    >
                      {item.title}
                    </span>
                    <Tooltip title="重命名">
                      <EditOutlined
                        style={{ color: '#999', flexShrink: 0 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          startEdit(item);
                        }}
                      />
                    </Tooltip>
                    <Popconfirm
                      title="删除该会话？"
                      description="将同时删除该会话的临时知识库"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                      onConfirm={() => handleDelete(item)}
                    >
                      <Tooltip title="删除">
                        <DeleteOutlined
                          style={{ color: '#999', flexShrink: 0 }}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Tooltip>
                    </Popconfirm>
                  </>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
