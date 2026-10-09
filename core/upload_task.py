"""
模块名：core.upload_task（异步上传任务状态存储，进程内）。

作用：
    大文件/文件夹上传转后台异步处理时，用 task_id 追踪一个上传批次的整体进度，
    供前端轮询 GET /file/status/{task_id}。
    - 用户侧仅能看到整体百分比（0-100）与最终状态（processing/success/failed）；
    - 各文件处理结果与分阶段信息只存于本模块（及后端日志），不暴露给用户；
    - 任务记录创建 1 小时后视为过期，查询时顺手清理，避免内存无限膨胀。

状态机：
    create_task 建任务为 processing -> 每个文件完成 update_file_result 累加
    completed/failed：出现任一失败立即置 failed 并收尾；否则当
    completed+failed 达到 total 时置 success。终态（success/failed）不可逆。

存储与并发：
    模块级字典 _task_store + threading.Lock；更新来自后台线程池工作线程，
    读取来自 FastAPI 请求线程，故所有读写均持锁。注意这是单机实现，
    多副本部署下轮询请求需落到同一副本（当前部署为单副本）。

主要成员：
    create_task / update_file_result / get_progress / cleanup_expired。

被谁使用（Grep upload_task）：
    仅 app/api/v1/files.py：
    - upload_file（异步分支）create_task、update_file_result；
    - 后台函数 _run_async_upload 每个文件完成后 update_file_result；
    - get_upload_status 端点 cleanup_expired + get_progress。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, Optional

# task_id -> 任务记录字典；进程内存储，重启即失效（客户端按“任务不存在”处理）
_task_store: Dict[str, dict] = {}
_lock = threading.Lock()

# 任务状态保留时长（秒），超时后清理
_TASK_TTL_SECONDS = 3600


def create_task(total: int) -> str:
    """创建一个新的异步上传任务。

    被谁调用：app/api/v1/files.py 的 upload_file，在判定为大上传
        （总大小 >10MB 或文件数 >20）转入异步分支时调用。
    参数：
        total: 本批次文件总数（来自 HTTP 上传的文件列表长度），
            作为进度分母与状态机终止判定依据。
    返回：str，uuid4 十六进制 task_id；去向为上传接口响应体的 task_id，
        前端凭此轮询进度。
    """
    task_id = uuid.uuid4().hex
    with _lock:
        # 初始状态机：processing，0 完成 0 失败
        _task_store[task_id] = {
            "task_id": task_id,
            "status": "processing",   # processing | success | failed
            "total": total,
            "completed": 0,
            "failed": 0,
            "message": "上传处理中...",
            "started_at": time.time(),
            "finished_at": None,
            "files": [],              # 仅后端调试用，不返回给用户
        }
    return task_id


def update_file_result(task_id: str, file_result: dict) -> None:
    """记录单个文件的处理结果，并驱动整体状态机更新。

    被谁调用：app/api/v1/files.py 的后台 _run_async_upload
        （每文件解析/embedding/入库完成或异常后）及 upload_file 中
        文件落盘即失败的分支。
    参数：
        task_id: 所属批次任务 id（create_task 返回）；
        file_result: 单文件结果，至少含
            {"original_name": str, "status": "success"/"fail", "message": str}，
            来源为 _process_saved_file/_process_one_file 的返回。
    返回：None；任务不存在（如已过期清理）时静默忽略。
    """
    with _lock:
        task = _task_store.get(task_id)
        if not task:
            return
        task["files"].append(file_result)
        # 按单文件结果累加成功/失败计数（进度仅以成功数计）
        if file_result.get("status") == "success":
            task["completed"] += 1
        else:
            task["failed"] += 1
        # 任一文件失败即整体失败（避免用户误以为全部可用）
        if task["failed"] > 0 and task["status"] == "processing":
            task["status"] = "failed"
            task["message"] = file_result.get("message", "文件处理失败")
            task["finished_at"] = time.time()
        # 无失败且全部文件已有结果 -> 成功收尾（completed+failed 必等于 total）
        elif task["completed"] + task["failed"] >= task["total"] and task["status"] == "processing":
            task["status"] = "success"
            task["finished_at"] = time.time()


def get_progress(task_id: str) -> Optional[dict]:
    """获取任务整体进度（用户侧视图）。

    被谁调用：app/api/v1/files.py 的 get_upload_status 端点
        （GET /file/status/{task_id}）。
    参数：
        task_id: 前端轮询携带的任务 id（HTTP 路径参数）。
    返回：Optional[dict]；任务存在时返回
        {"status": processing|success|failed, "progress": 0-100,
        "message": str}（刻意不返回 files 明细）；任务不存在或已过期
        返回 None，由端点转为 404“任务不存在或已过期”。
    """
    with _lock:
        task = _task_store.get(task_id)
        if not task:
            return None
        total = task["total"]
        completed = task["completed"]
        # 整体百分比只统计成功完成数；total 为 0 时按 0 防除零
        progress = int(completed / total * 100) if total > 0 else 0
        return {
            "status": task["status"],
            "progress": progress,
            "message": task["message"],
        }


def cleanup_expired() -> int:
    """清理已超时的任务记录，防止进程内字典无限膨胀。

    被谁调用：app/api/v1/files.py 的 get_upload_status 端点
        （每次轮询顺手清理一次）。
    返回：int，本轮删除的过期任务数量。
    判定口径：started_at 存在且距当前时间超过 _TASK_TTL_SECONDS（1 小时）；
        终态/非终态任务一视同仁，过期即删。
    """
    now = time.time()
    removed = 0
    with _lock:
        # 先在锁内快照出所有过期 task_id，再统一删除，避免遍历中改字典
        expired = [
            tid for tid, t in _task_store.items()
            if t["started_at"] and now - t["started_at"] > _TASK_TTL_SECONDS
        ]
        for tid in expired:
            del _task_store[tid]
            removed += 1
    return removed
