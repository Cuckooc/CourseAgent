"""
模块：file_control.py
作用：知识库文件上传 HTTP 接口，负责安全校验、落盘、解析、脱敏、embedding 入库与异步任务进度查询。
主要成员：
- file_router：文件路由对象（prefix=/file）；
- 模块常量：UPLOAD_DIR（上传根目录）、_VALID_SCOPES（合法知识库范围）、
  _ASYNC_TOTAL_BYTES/_ASYNC_FILE_COUNT（异步大上传阈值）、_TEXT_FORBIDDEN_MAGICS（伪装文本黑名单魔数）；
- _validate_magic：按扩展名做 magic bytes 内容校验；
- _save_one：保存单个上传文件到目标目录（白名单/分块写入/大小限制/魔数校验）；
- _upload_executor / _async_upload_executor：同步并行处理池与异步大上传调度池；
- _process_saved_file：处理已落盘文件（解析/脱敏/embedding/入库）；
- _process_one_file：单文件完整处理（落盘 + 处理），线程池 worker 入口；
- _run_async_upload：大上传后台任务（提交并行处理并更新进度、批次 flush）；
- upload_file：上传路由（同步小上传 / 异步大上传两条路径）；
- get_upload_status：异步上传任务进度查询路由。
被谁使用：由 control/app.py 通过 `from app.api.v1.files import file_router` 导入并
          app.include_router 注册；路由由 HTTP 客户端（web/frontend 上传组件）调用，非内部调用。
安全与范围：
- 必须登录（JWT）；
- 扩展名为白名单校验 + magic bytes 内容校验（拦截伪装扩展名的二进制/可执行文件）；
- 文件名使用 uuid 重命名，杜绝路径穿越；
- 大小限制（默认 50MB/文件），分块写入，超限立即中止；
- 支持单文件或文件夹（多文件）上传；
- 多文件在线程池中【并行】处理（解析/脱敏/embedding 并行，向量库写入按文件批次串行），
  每个文件独立读取/切分/构建元数据，文件内容互不串块；
- scope 参数：private（默认，用户私有）/ temp（会话临时，需 session_id）/ public（teacher/admin）；
- 入库前对文本执行脱敏（手机号/身份证/邮箱/银行卡/姓名）。
"""
import asyncio
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.infrastructure.persistence.repositories.knowledge import build_stored_filename
from app.application.files.file_service import FileService
from app.application.files.mask_service import mask_text
from app.application.files.preference_service import extract_preferences
from app.application.ports.vector import get_temp_store
from app.application.ports.vector import flush_persistent_index
from core import upload_task
from core.config import settings
from app.auth.guards import get_current_user
from app.auth.rate_limit import user_rate_limit
from core.responses import BizException

# 本模块日志器：记录偏好提取失败、后台 flush 失败等非阻断异常
logger = logging.getLogger(__name__)
# 文件路由：prefix=/file，由 control/app.py 的 app.include_router(file_router) 注册
file_router = APIRouter(prefix="/file", tags=["file control"])

# 上传文件物理落盘根目录：来自配置 settings.UPLOAD_DIR（public/private 文件落此目录）
UPLOAD_DIR = str(settings.UPLOAD_DIR)
# 合法知识库范围集合：upload_file 路由据此校验前端传入的 scope 表单字段
_VALID_SCOPES = {"private", "temp", "public"}

# 大上传阈值常量：总大小超过 10MB 或文件数超过 20 时走异步后台处理
_ASYNC_TOTAL_BYTES = 10 * 1024 * 1024
_ASYNC_FILE_COUNT = 20

# 伪装成文本的常见二进制/可执行文件魔数元组（.txt/.md 等文本类扩展名的负向校验依据）
_TEXT_FORBIDDEN_MAGICS = (
    b"MZ",            # Windows PE（exe/dll）
    b"\x7fELF",       # Linux 可执行
    b"PK\x03\x04",    # zip 容器（docx/xlsx/pptx/jar）
    b"PK\x05\x06",    # 空 zip
    b"%PDF",          # 伪装成文本的 PDF
    b"\x1f\x8b",      # gzip
    b"Rar!",          # rar
    b"7z\xbc\xaf\x27\x1c",  # 7z
)


def _validate_magic(ext: str, file_path: str) -> None:
    """按扩展名校验文件头部 magic bytes，拦截伪装成文本/文档的可执行或二进制文件。

    被谁调用：模块内部函数，仅由 _save_one 落盘完成后调用。
    参数：
    - ext：已小写化的扩展名（含点，如 ".pdf"），来源 _save_one 对原始文件名的切分；
    - file_path：已落盘文件的物理路径，来源 _save_one。
    返回：无返回值；通过即视为内容与扩展名相符。
    异常：
    - BizException(400)：空文件、PDF 文件头不为 %PDF-、命中黑名单魔数、或头部含 NUL 字节。
    """
    with open(file_path, "rb") as f:
        head = f.read(1024)
    if not head:
        raise BizException("文件内容为空", http_status=400)
    if ext == ".pdf":
        # PDF 走正向校验：必须以 %PDF- 开头
        if not head.startswith(b"%PDF-"):
            raise BizException("文件内容与扩展名 .pdf 不符", http_status=400)
        return
    # 文本类扩展名走负向校验：命中任一可执行/压缩魔数即拒绝
    for magic in _TEXT_FORBIDDEN_MAGICS:
        if head.startswith(magic):
            raise BizException("文件内容与扩展名不符，疑似可执行/二进制文件", http_status=400)
    # NUL 字节是二进制文件的典型特征，文本文件不应出现
    if b"\x00" in head:
        raise BizException("文件内容与扩展名不符，疑似二进制文件", http_status=400)


def _save_one(file: UploadFile, target_dir: str, user_id: int = None) -> tuple:
    """保存单个文件到指定目录，返回 (stored_name, file_path, original_name)。

    被谁调用：模块内部函数，由 _process_one_file（小上传线程池任务）与
              upload_file 的异步大上传落盘阶段调用。
    参数：
    - file：FastAPI UploadFile 对象，来源前端 multipart/form-data 上传；
    - target_dir：落盘目标目录，来源上游路由（temp 为会话临时目录，其余为 UPLOAD_DIR）；
    - user_id：当前登录用户 ID，来源 JWT，用于生成存储文件名；默认 None。
    返回：三元组 (stored_name, file_path, original_name)，交给 _process_saved_file 继续处理。
    异常：
    - BizException(400)：扩展名不在白名单；
    - BizException(413)：写入字节数超过 UPLOAD_MAX_BYTES（同时删除半成品文件）；
    - BizException(500)：落盘过程其他 IO 异常（同时删除半成品文件）；
      magic bytes 校验失败时异常由 _validate_magic 抛出并清理文件。
    存储命名：{user_id}_{清洗后原始文件名}_{hash32}{ext}——用户ID+文件名+hash 唯一标识。
    """
    original_name = file.filename or ""
    _, ext = os.path.splitext(original_name)
    ext = ext.lower()
    if ext not in settings.UPLOAD_ALLOWED_EXT_SET:
        raise BizException(
            f"不支持的文件类型：{ext or '未知'}，仅支持 {','.join(sorted(settings.UPLOAD_ALLOWED_EXT_SET))}",
            http_status=400,
        )
    os.makedirs(target_dir, exist_ok=True)
    stored_name = build_stored_filename(user_id, original_name, ext)
    file_path = os.path.join(target_dir, stored_name)

    # 分块流式写入：每块 1MB，边写边累计字节数，超限立即删除半成品并中止（避免整文件读入内存）
    max_bytes = settings.UPLOAD_MAX_BYTES
    written = 0
    try:
        with open(file_path, "wb") as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    f.close()
                    os.remove(file_path)
                    raise BizException(f"文件大小超过限制（{settings.UPLOAD_MAX_MB}MB）", http_status=413)
                f.write(chunk)
    except BizException:
        raise
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise BizException(f"文件保存失败: {str(e)}", http_status=500)

    try:
        _validate_magic(ext, file_path)
    except BizException:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    return stored_name, file_path, original_name


# 多文件并行处理池（模块级单例）：worker 内执行 落盘→解析→脱敏→embedding（可并行），
# 向量库写入由 app/infrastructure/vector_store/persistent 的进程锁串行化。worker 数来自配置 UPLOAD_WORKERS。
_upload_executor = ThreadPoolExecutor(
    max_workers=settings.UPLOAD_WORKERS, thread_name_prefix="upload-worker"
)

# 异步大上传后台调度池（模块级单例）：仅负责把整批文件提交到 _upload_executor 并汇总进度，
# 实际解析/embedding 仍复用 _upload_executor 并行执行。
_async_upload_executor = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="async-upload"
)


def _process_saved_file(index: int, stored_name: str, file_path: str, original_name: str,
                        scope: str, user_id: int, session_id: Optional[int],
                        dedup_strategy: str = None, update_strategy: str = None) -> tuple:
    """处理已落盘的文件（parse/mask/embed/store），不负责落盘。

    被谁调用：模块内部函数，由 _process_one_file（小上传）与 _run_async_upload（大上传）
              提交到 _upload_executor 在线程池中调用。
    参数：
    - index：文件在本次上传批次中的原始下标，来源上游，用于按上传顺序回填结果；
    - stored_name：落盘存储文件名，来源 _save_one；
    - file_path：已落盘物理路径，来源 _save_one；
    - original_name：用户上传时的原始文件名，来源 _save_one；
    - scope：知识库范围 private/temp/public，来源前端表单并经路由校验；
    - user_id：当前登录用户 ID，来源 JWT；
    - session_id：temp 范围所属会话 ID，来源前端表单（其他范围为 None）；
    - dedup_strategy：去重策略 full/filename，来源前端表单（None 用全局配置）；
    - update_strategy：更新策略 replace/version，来源前端表单（None 用全局配置）。
    返回：二元组 (index, result_dict)；result_dict 含 original_name 与
          status(success/skipped/fail)，成功附 stored_name/scope/doc_type，
          失败附 message；skipped/fail 时负责清理已落盘的物理文件。
    """
    try:
        file_service = FileService(file_path)
        if scope == "temp":
            result = file_service.process_temp_file(
                file_path, user_id, session_id, original_name
            )
        else:
            result = file_service.process_file(
                file_path, scope, user_id, None, original_name,
                dedup_strategy=dedup_strategy, update_strategy=update_strategy,
            )
        if result.get("success", False):
            if scope == "temp":
                # 临时文件入库后顺带提取用户偏好：原文经脱敏再抽取，失败不阻断上传主流程
                try:
                    from app.infrastructure.document.file import pdf_text
                    raw_text = pdf_text(file_path)
                    masked_text = mask_text(raw_text)
                    extract_preferences(masked_text, user_id)
                except Exception as e:
                    logger.warning("preference extraction skipped: %s", e)
            # 去重跳过：内容已存在，清理刚落盘的重复物理文件
            if result.get("skipped"):
                if os.path.exists(file_path):
                    os.remove(file_path)
                return index, {
                    "original_name": original_name,
                    "status": "skipped",
                    "scope": scope,
                    "message": result.get("reason", "内容已存在"),
                }
            return index, {
                "original_name": original_name,
                "stored_name": stored_name,
                "status": "success",
                "scope": scope,
                "doc_type": result.get("doc_type", "pure_text"),
            }
        # 解析失败：清理已落盘文件
        if os.path.exists(file_path):
            os.remove(file_path)
        return index, {
            "original_name": original_name,
            "status": "fail",
            "message": result.get("error", "解析失败"),
        }
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        return index, {"original_name": original_name, "status": "fail", "message": str(e)}


def _process_one_file(index: int, file: UploadFile, target_dir: str, scope: str,
                      user_id: int, session_id: Optional[int],
                      dedup_strategy: str = None, update_strategy: str = None) -> tuple:
    """单个文件的完整处理（在线程池 worker 中执行）。

    被谁调用：模块内部函数，由 upload_file 小上传路径通过 loop.run_in_executor
              提交到 _upload_executor 调用。
    参数：
    - index：文件在上传批次中的原始下标，来源 upload_file，用于按序回填结果；
    - file：FastAPI UploadFile 对象，来源前端上传；
    - target_dir / scope / user_id / session_id / dedup_strategy / update_strategy：
      含义与来源同 _process_saved_file，由 upload_file 透传。
    返回：二元组 (index, result_dict)；落盘阶段抛 BizException 时不继续处理，
          直接返回携带失败 message 的结果字典。

    每个文件使用独立的文件句柄、独立的文本/切片/父子 Document 列表，
    仅最终向量库写入走全局锁串行，杜绝多线程下文件内容错乱/串块。
    """
    try:
        stored_name, file_path, original_name = _save_one(file, target_dir, user_id)
    except BizException as e:
        return index, {"original_name": file.filename, "status": "fail", "message": str(e)}

    return _process_saved_file(index, stored_name, file_path, original_name, scope, user_id, session_id,
                               dedup_strategy=dedup_strategy, update_strategy=update_strategy)


def _run_async_upload(task_id: str, saved_files: list, scope: str,
                      user_id: int, session_id: Optional[int],
                      dedup_strategy: str = None, update_strategy: str = None) -> None:
    """后台异步处理已落盘的文件列表，逐文件更新任务进度，结束后 flush 持久库。

    被谁调用：模块内部函数，由 upload_file 大上传路径提交到 _async_upload_executor 调用。
    参数：
    - task_id：异步任务 ID，来源 upload_task.create_task，用于回写每文件进度；
    - saved_files：已成功落盘的三元组 (stored_name, file_path, original_name) 列表；
    - scope / user_id / session_id / dedup_strategy / update_strategy：
      含义与来源同 _process_saved_file，透传给每个 worker。
    返回：无返回值；结果通过 upload_task.update_file_result 写入任务进度，供前端轮询。
    """
    # 把每个已落盘文件提交到并行处理池，保留 future→下标 映射以回填进度
    futures = {
        _upload_executor.submit(
            _process_saved_file, idx, stored_name, file_path, original_name, scope, user_id, session_id,
            dedup_strategy, update_strategy,
        ): idx
        for idx, (stored_name, file_path, original_name) in enumerate(saved_files)
    }
    # 按完成顺序（而非提交顺序）回收 worker 结果，每完成一个文件立即刷新任务进度
    for future in as_completed(futures):
        idx = futures[future]
        try:
            _, result = future.result()
        except Exception as e:
            original_name = saved_files[idx][2]
            result = {"original_name": original_name, "status": "fail", "message": str(e)}
        upload_task.update_file_result(task_id, result)
    # 批次级 flush：所有文件处理完毕后一次性落盘持久库
    if scope != "temp":
        try:
            flush_persistent_index()
        except Exception as e:
            logger.exception("flush_persistent_index failed in async upload: %s", e)


@file_router.post(
    "/path",
    dependencies=[Depends(user_rate_limit(5, 60))],  # 上传含 embedding 重调用：5 次/分钟
)
async def upload_file(
    files: List[UploadFile] = File(...),
    scope: str = Form("private"),
    session_id: Optional[int] = Form(None),
    dedup_strategy: Optional[str] = Form(None),
    update_strategy: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """上传单文件或文件夹（多文件）到知识库。

    HTTP 方法+路径：POST /file/path（multipart/form-data）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；
                user_rate_limit(5, 60) 依赖按登录用户限流（上传含 embedding 重调用：5 次/分钟）。
    被谁调用：由 HTTP 客户端（web/frontend 上传组件）调用，非内部调用。
    参数：
    - files：表单文件字段，一个或多个 UploadFile，来源前端选择的文件/文件夹；
    - scope：表单字段，private（默认，当前用户私有）/ temp（当前会话临时，需 session_id）/
      public（仅 teacher/admin），来源前端；
    - session_id：表单字段，temp 范围必填的会话 ID，来源前端当前会话；
    - dedup_strategy：表单字段，full（全程扫描）/ filename（文件名扫描，默认），不传用全局配置；
    - update_strategy：表单字段，replace（先删后增+回滚）/ version（版本标记，默认），不传用全局配置；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息（含 user_id/role）。
    返回：HTTP JSON 响应。小上传同步返回 {status:"success"/"partial", message, files:[...]}；
          大上传（总大小 >10MB 或文件数 >20）先同步落盘并立即返回
          {status:"processing", task_id}，后续经 /file/status/{task_id} 轮询。
    异常：
    - BizException(400)：非法 scope、temp 缺 session_id、或小上传全部文件处理失败；
    - BizException(403)：非 teacher/admin 上传公共库，或 admin 上传私有/临时库，或会话归属校验失败。
    """
    scope = (scope or "private").lower()
    if scope not in _VALID_SCOPES:
        raise BizException(f"非法的知识库范围：{scope}", http_status=400)
    role = current_user.get("role")
    if scope == "public":
        # 公共知识库仅 teacher/admin 可写入（teacher 为课程内容角色，admin 为超管保留）
        if role not in ("teacher", "admin"):
            raise BizException("没有权限上传公共知识库", http_status=403)
    else:
        # admin 为纯管理角色：无私有/临时知识库（无对话场景，临时库依附会话）
        if role == "admin":
            raise BizException("管理员账号不支持私有/临时知识库", http_status=403)
    user_id = current_user.get("user_id")
    if scope == "temp" and not session_id:
        raise BizException("临时知识库必须指定 session_id", http_status=400)
    if scope == "temp":
        # 会话归属校验：会话号为 per-user 序列，仅允许往自己的会话上传临时知识库
        from app.infrastructure.persistence.repositories.session import SessionDAO

        if not SessionDAO().is_session_owner(user_id, session_id):
            raise BizException("会话不存在或无权限操作", http_status=403)

    temp_store = get_temp_store()
    # temp 文件物理落盘到会话临时目录（目录含用户ID：temp/<uid>_<sid>，会话结束即删），
    # public/private 落 UPLOAD_DIR；会话号为 per-user 序列，目录/内存库必须按 (uid,sid) 隔离
    target_dir = (
        str(temp_store.ensure_session_dir(user_id, session_id)) if scope == "temp" else UPLOAD_DIR
    )

    # 判断是否走异步后台处理：总大小 > 10MB 或文件数 > 20
    total_size = sum((f.size or 0) for f in files)
    if total_size > _ASYNC_TOTAL_BYTES or len(files) > _ASYNC_FILE_COUNT:
        # 异步路径：先同步落盘所有文件，立即返回 task_id，后台再解析/embedding/入库
        saved_files = []
        task_id = upload_task.create_task(len(files))
        for idx, file in enumerate(files):
            try:
                stored_name, file_path, original_name = _save_one(file, target_dir, user_id)
                saved_files.append((stored_name, file_path, original_name))
            except BizException as e:
                # 落盘失败：直接记为该文件失败，不进入后台处理
                upload_task.update_file_result(
                    task_id,
                    {"original_name": file.filename, "status": "fail", "message": str(e)},
                )
            except Exception as e:
                upload_task.update_file_result(
                    task_id,
                    {"original_name": file.filename, "status": "fail", "message": f"文件保存失败: {e}"},
                )
        if saved_files:
            _async_upload_executor.submit(
                _run_async_upload, task_id, saved_files, scope, user_id, session_id,
                dedup_strategy, update_strategy,
            )
        return {
            "status": "processing",
            "message": f"大上传已转后台处理，共 {len(files)} 个文件",
            "task_id": task_id,
        }

    # 小上传：保持现有同步流程
    # 多文件并行处理：每个文件一个线程池任务（落盘/解析/embedding 并行，向量库写入加锁串行）。
    # 带原始下标提交，完成后按上传顺序回填 results，保证响应 files 数组顺序与请求一致。
    loop = asyncio.get_running_loop()
    futures = [
        loop.run_in_executor(
            _upload_executor,
            _process_one_file,
            idx,
            file,
            target_dir,
            scope,
            user_id,
            session_id,
            dedup_strategy,
            update_strategy,
        )
        for idx, file in enumerate(files)
    ]
    settled = await asyncio.gather(*futures)
    results = [item for _, item in sorted(settled, key=lambda x: x[0])]

    # 批次级统一 flush：所有文件 add 完毕后一次性把持久库残批并入索引并落盘，
    # 替代每文件 flush，减少锁占用与 SQLite 写次数。
    if scope != "temp":
        flush_persistent_index()

    success_count = sum(1 for r in results if r["status"] == "success")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    fail_count = len(results) - success_count - skipped_count
    if success_count == 0 and skipped_count == 0:
        # 全部失败：返回第一个失败的错误信息
        first_fail = next((r for r in results if r["status"] not in ("success", "skipped")), {})
        raise BizException(first_fail.get("message", "文件处理失败"), http_status=400)
    return {
        "status": "success" if fail_count == 0 else "partial",
        "message": f"成功 {success_count}/{len(results)} 个文件" + (f"，跳过 {skipped_count} 个重复" if skipped_count else ""),
        "files": results,
    }


@file_router.get("/status/{task_id}")
async def get_upload_status(task_id: str):
    """查询异步上传任务的整体进度。

    HTTP 方法+路径：GET /file/status/{task_id}。
    鉴权与限流：本端点未声明鉴权/限流依赖（task_id 为随机任务标识，仅能查询本人刚发起的任务）。
    被谁调用：由 HTTP 客户端（web/frontend 大上传后轮询）调用，非内部调用。
    参数：
    - task_id：路径参数，异步上传任务标识，来源 upload_file 大上传响应下发、前端原样回传。
    返回：进度字典 {status: processing|success|failed, progress: 0-100, message: str}，
          作为 HTTP JSON 响应给前端；同时顺手清理过期任务避免内存膨胀。
    异常：任务不存在或已过期时抛 BizException(404)。
    """
    # 顺手清理过期任务，避免内存膨胀
    upload_task.cleanup_expired()
    progress = upload_task.get_progress(task_id)
    if progress is None:
        raise BizException("任务不存在或已过期", http_status=404)
    return progress
