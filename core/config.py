"""
模块名：core.config

作用：
    应用级配置中心。进程启动时从 env/config.env 加载环境变量（与数据库配置同一文件），
    由 Settings 类以类属性形式集中暴露全部运行参数；所有密钥/环境相关参数均通过
    环境变量注入，禁止硬编码。env/config.env 为本地文件，禁止提交版本库。

主要成员：
    - Settings：配置类，字段在类定义时（导入期）从环境变量读取并固化为 Final 类属性，
      另提供 CORS_ORIGINS_LIST / UPLOAD_DIR / IS_PROD / LOG_JSON 等派生 property；
    - settings：模块级全局单例，导入本模块时完成唯一一次实例化，全仓共用；
    - BASE_DIR：项目根目录（core 的上一级），路径类常量的基准；
    - ENV_PATH：env/config.env 的绝对路径，模块导入时即执行 load_dotenv。

被谁使用（Grep "from core.config import" 确认，共 30+ 模块）：
    - core 内：security / redis_client / mailer / usage / sql_guard / account_guard /
      delete_guard / degradation_alert / purge_scheduler 等；
    - control：app / login_control / file_control 等；
    - service：chat_service / agent_service / file_service / knowledge_service 等；
    - multi_agent、memory、tools、dao、db 等子包。
    其中 audit.py / degradation_alert.py 仅导入 BASE_DIR 常量。
"""
import os
from pathlib import Path
from typing import Final, List

from dotenv import load_dotenv

# 模块级常量：项目根目录（本文件位于 <根>/core/config.py，取 parent.parent）。
# 导入期确定；日志目录、上传目录、env 文件路径等均以它为基准拼接。
BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent
# 模块级常量：环境变量文件路径（<根>/env/config.env，本地文件、禁止入库）。
ENV_PATH: Final[Path] = BASE_DIR / "env" / "config.env"
# 导入期一次性把 config.env 注入 os.environ；其后 Settings 字段统一用 os.getenv 读取。
load_dotenv(dotenv_path=ENV_PATH)


class Settings:
    """应用配置类（纯静态类属性容器，不做实例化传参）。

    作用：类定义执行期间（模块导入期）逐字段调用 os.getenv 读取环境变量并固化为
    Final 类属性；缺失的密钥类字段（JWT_SECRET）直接 raise RuntimeError 阻断启动。
    实例化位置：本文件末尾的模块级单例 ``settings = Settings()``，全仓唯一实例，
    各模块通过 ``from core.config import settings`` 共享读取（不在业务代码中再次实例化）。
    配置来源：真实环境变量优先，其次 env/config.env（load_dotenv 不会覆盖已存在的环境变量）。
    """
    # ================= 运行环境 =================
    # app_env：运行环境 dev / staging / prod，默认 dev（开发态默认宽松配置）。
    # 被 control/app.py 经 IS_PROD property 读取（prod 启动时强制校验密钥、决定日志 JSON 形态）。
    APP_ENV: Final[str] = os.getenv("app_env", "dev")

    # ================= JWT 认证 =================
    # 环境变量 jwt_secret：JWT 签名密钥。【密钥类字段】只能来自真实环境变量或
    # env/config.env 本地文件，禁止入库、禁止硬编码默认值上线；为空则启动直接报错。
    # 被 core/security.py 的 create_access_token / decode_token 读取（签发与校验共用）。
    JWT_SECRET: Final[str] = os.getenv("jwt_secret", "")
    if not JWT_SECRET:
        raise RuntimeError("jwt_secret 未配置：请在 env/config.env 或环境变量中设置")
    # jwt_algorithm：签名算法，默认 HS256；被 core/security.py 读取。
    JWT_ALGORITHM: Final[str] = os.getenv("jwt_algorithm", "HS256")
    # access_token_expire_minutes：访问令牌有效期（分钟），默认 720（12 小时）；
    # 被 core/security.py 签发 token 时读取。
    ACCESS_TOKEN_EXPIRE_MINUTES: Final[int] = int(os.getenv("access_token_expire_minutes", "720"))

    # ================= Redis（可选） =================
    # redis_url：Redis 连接串，默认空。多副本部署必须配置；
    # 为空或连接失败时，限流（core/deps.py）/登录锁定（core/account_guard.py）/
    # 删除确认令牌（core/delete_guard.py）/分布式锁（core/locks.py）及记忆模块自动降级为进程内存实现。
    # 主要被 core/redis_client.py 读取并创建连接单例。
    REDIS_URL: Final[str] = os.getenv("redis_url", "")

    # ================= 可观测性 =================
    # log_level：全局日志级别，默认 INFO；被 control/app.py 读取后传给 logging_config.setup_logging。
    LOG_LEVEL: Final[str] = os.getenv("log_level", "INFO")
    # log_json：auto=prod 用 JSON、dev 用彩色文本；可强制 true/false；
    # 经 LOG_JSON property 解析后被 control/app.py 读取。
    LOG_JSON_RAW: Final[str] = os.getenv("log_json", "auto")
    # metrics_enabled：是否启用 Prometheus 指标与 /metrics 端点，默认 true；
    # 被 control/app.py 读取（决定是否注册 MetricsMiddleware 与暴露端点）。
    METRICS_ENABLED: Final[bool] = os.getenv("metrics_enabled", "true").lower() in (
        "1",
        "true",
        "yes",
    )

    # ================= CORS 跨域 =================
    # cors_origins：允许来源，逗号分隔；* 表示全部允许（默认值，仅建议开发环境使用）。
    # 经 CORS_ORIGINS_LIST property 拆分为列表，被 control/app.py 的 CORSMiddleware 读取。
    CORS_ORIGINS: Final[str] = os.getenv("cors_origins", "*")

    # ================= 文件上传 =================
    # 被 app/api/v1/files.py（HTTP 大小/扩展名校验、线程池）、app/application/files/file_service.py、
    # app/application/knowledge/knowledge_service.py、app/domain/tools/function_tools.py、app/infrastructure/vector_store/temp_store.py 读取。
    # upload_max_mb：单文件大小上限（MB），默认 50；经 UPLOAD_MAX_BYTES property 换算后用于 413 拦截。
    UPLOAD_MAX_MB: Final[int] = int(os.getenv("upload_max_mb", "50"))
    # upload_allowed_ext：允许上传的扩展名，逗号分隔，默认 ".pdf,.txt,.md"
    # （仅放行有解析器的类型，docx/pptx 解析器未实现）；经 UPLOAD_ALLOWED_EXT_SET property 转集合。
    UPLOAD_ALLOWED_EXT: Final[str] = os.getenv(
        "upload_allowed_ext", ".pdf,.txt,.md"  # 仅放行有解析器的类型（docx/pptx 解析器未实现）
    )
    # upload_workers：多文件/文件夹上传的并行处理线程数（解析+embedding 并行，向量库写入仍串行）。
    # 受 DashScope embedding 并发额度约束，默认 4 较稳妥；max(1, ...) 保证至少 1 个线程。
    UPLOAD_WORKERS: Final[int] = max(1, int(os.getenv("upload_workers", "4")))

    # ================= 记忆模块 =================
    # 本组字段全部由 memory 子包读取：short_term_* → app/domain/memory/short_term.py、app/domain/memory/long_term.py；
    # context_* → app/domain/memory/context_app.domain.memory.py；session_auto_rollover_* → app/domain/memory/session_rollover.py；
    # profile_* → app/domain/memory/profile_service.py；session_keyword_* → app/domain/memory/session_keyword_service.py。
    # 短期记忆：会话级滑动保留时间（秒）。会话在该时间内无新对话即过期清理；
    # 每次在原会话继续对话都会重新计时（滑动过期）。
    SHORT_TERM_TTL_SECONDS: Final[int] = int(os.getenv("short_term_ttl_seconds", "1800"))
    # 短期记忆每会话最多缓存的消息条数（防止热点会话无限增长）
    SHORT_TERM_MAX_MESSAGES: Final[int] = int(os.getenv("short_term_max_messages", "40"))
    # 短期记忆 → 长期记忆落库：剩余 TTL 低于该阈值（会话已静默临近过期）即批量写 MySQL
    # 并删除 Redis 短期记忆（长期记忆由短期记忆转变而来，对话期间不写数据库）
    SHORT_TERM_FLUSH_TTL_SECONDS: Final[int] = int(os.getenv("short_term_flush_ttl_seconds", "300"))
    # 落库扫描周期（秒）：后台任务每隔该时间扫描一次短期记忆
    SHORT_TERM_FLUSH_INTERVAL_SECONDS: Final[int] = int(
        os.getenv("short_term_flush_interval_seconds", "60")
    )

    # 上下文记忆：近期原文保留轮数（1 轮 = 1 条 user + 1 条 assistant）
    CONTEXT_ROUNDS_SPARSE: Final[int] = int(os.getenv("context_rounds_sparse", "5"))  # 内容少：5轮
    CONTEXT_ROUNDS_DENSE: Final[int] = int(os.getenv("context_rounds_dense", "3"))  # 内容多：3轮
    # 判定"内容多/少"：近期保留窗口内原文平均每轮字符数阈值
    CONTEXT_DENSE_CHARS_PER_ROUND: Final[int] = int(os.getenv("context_dense_chars_per_round", "800"))
    # 上下文窗口字符上限（主要压缩触发条件：摘要+近期原文达到上限即压缩更早信息）
    CONTEXT_WINDOW_CHARS: Final[int] = int(os.getenv("context_window_chars", "6000"))
    # 上下文记忆自身保留时间（秒）：随会话滑动，略长于短期记忆
    CONTEXT_TTL_SECONDS: Final[int] = int(os.getenv("context_ttl_seconds", "7200"))

    # 会话自动滚换（长对话漂移治理）：实测单会话约 12 轮后模型对长历史的利用开始
    # 劣化（机械复读摘要/事实泛化/回答膨胀）。达到设定轮数后自动新建会话并把
    # 最近 N 轮原文、早期摘要、会话关键词、会话临时知识库整体迁移，用户无感续聊。
    SESSION_AUTO_ROLLOVER_ENABLED: Final[bool] = os.getenv(
        "session_auto_rollover_enabled", "true"
    ).lower() in ("1", "true", "yes")
    SESSION_AUTO_ROLLOVER_TURNS: Final[int] = int(os.getenv("session_auto_rollover_turns", "15"))

    # 用户画像：手动/自动变更先暂存 Redis，连续 N 天无更新才落 MySQL
    PROFILE_PENDING_TTL_DAYS: Final[int] = int(os.getenv("profile_pending_ttl_days", "7"))
    # 画像自动提取节流：同一用户两次提取的最小间隔（秒）
    PROFILE_EXTRACT_INTERVAL_SECONDS: Final[int] = int(os.getenv("profile_extract_interval_seconds", "600"))

    # 会话关键词累积上限（单会话最多保留的关键词数）
    SESSION_KEYWORD_MAX: Final[int] = int(os.getenv("session_keyword_max", "100"))
    # 注入模型输入的关键词前缀字符上限
    SESSION_KEYWORD_INJECT_CHARS: Final[int] = int(os.getenv("session_keyword_inject_chars", "200"))
    # 关键词后台落库周期（秒）
    SESSION_KEYWORD_FLUSH_INTERVAL: Final[int] = int(os.getenv("session_keyword_flush_interval", "600"))

    # ================= 成本治理（P1） =================
    # 被 core/usage.py 读取（token 记账与限额拦截）。
    # 单用户 LLM token 预算（prompt+completion 合计；0 = 不限制）。
    # 达到限额后对话接口返回 429，次日/次月自动重置（Redis 键按天/月滚动）。
    # llm_daily_token_limit：单日预算，默认 0（不限）。
    LLM_DAILY_TOKEN_LIMIT: Final[int] = int(os.getenv("llm_daily_token_limit", "0"))
    # llm_monthly_token_limit：单月预算，默认 0（不限）。
    LLM_MONTHLY_TOKEN_LIMIT: Final[int] = int(os.getenv("llm_monthly_token_limit", "0"))

    # ================= 账号安全（P1） =================
    # 登录失败锁定：窗口期内同一用户名失败达 LOGIN_MAX_FAILURES 次后临时锁定。
    # 仅 Redis 可用时生效（降级放行，有限流兜底）。
    # 被 core/account_guard.py 与 app/api/v1/auth.py 读取。
    # login_max_failures：锁定阈值（次），默认 5。
    LOGIN_MAX_FAILURES: Final[int] = int(os.getenv("login_max_failures", "5"))
    # login_lock_window_seconds：计数窗口/锁定时长（秒），默认 900（15 分钟）。
    LOGIN_LOCK_WINDOW_SECONDS: Final[int] = int(os.getenv("login_lock_window_seconds", "900"))

    # ================= 数据库（MySQL） =================
    # 被 db/session.py 读取并拼接 SQLAlchemy 连接串；dao/user.py 亦有读取。
    # 注意下方环境变量名为历史简写（host/port/user/password/database/charset）。
    # host：数据库主机，默认 localhost。
    DB_HOST: Final[str] = os.getenv("host", "localhost")
    # port：数据库端口，默认 3306。
    DB_PORT: Final[int] = int(os.getenv("port", "3306"))
    # user：数据库账号，默认 root（生产环境应使用最小权限账号）。
    DB_USER: Final[str] = os.getenv("user", "root")
    # password：【密钥类字段】数据库密码，只能来自环境变量或 env/config.env 本地文件，
    # 禁止入库、禁止硬编码；默认空。被 db/session.py 经 URL 编码后拼入连接串。
    DB_PASSWORD: Final[str] = os.getenv("password", "")
    # database：库名，默认空（由 env/config.env 显式指定）。
    DB_NAME: Final[str] = os.getenv("database", "")
    # charset：连接字符集，默认 utf8mb4（支持 emoji 等四字节字符）。
    DB_CHARSET: Final[str] = os.getenv("charset", "utf8mb4")

    # ================= SQL 安全 =================
    # 被 core/sql_guard.py 读取（DAO 层查询前的安全护栏）。
    # sql_max_rows：单次查询最大返回行数（防止全表扫描拖垮数据库），默认 10000。
    SQL_MAX_ROWS: Final[int] = int(os.getenv("sql_max_rows", "10000"))
    # sql_tautology_check：WHERE 子句永真条件检测开关（1=1 等），检测到则阻止操作，默认 true。
    SQL_TAUTOLOGY_CHECK: Final[bool] = os.getenv("sql_tautology_check", "true").lower() in (
        "1", "true", "yes",
    )

    # ================= 软删除与定时清理 =================
    # 被 core/purge_scheduler.py 的后台定时清理任务读取。
    # account_deletion_grace_days：注销账户后宽限天数（到期后硬删除所有数据），默认 7。
    ACCOUNT_DELETION_GRACE_DAYS: Final[int] = int(os.getenv("account_deletion_grace_days", "7"))
    # soft_delete_retention_days：软删除记录保留天数（到期后从数据库彻底清除），默认 1095（3 年）。
    SOFT_DELETE_RETENTION_DAYS: Final[int] = int(os.getenv("soft_delete_retention_days", "1095"))
    # purge_interval_hours：定时清理扫描间隔（小时），默认 6。
    PURGE_INTERVAL_HOURS: Final[int] = int(os.getenv("purge_interval_hours", "6"))

    # ================= 知识库去重与版本管理 =================
    # 被 app/application/knowledge/knowledge_service.py 等知识库服务读取（上传去重与文档更新链路）。
    # dedup_strategy：去重策略，full=全程扫描 / filename=文件名扫描（默认，更快）。
    DEDUP_STRATEGY: Final[str] = os.getenv("dedup_strategy", "filename")
    # update_strategy：更新策略，replace=先删后增+回滚 / version=版本标记 is_latest（默认）。
    UPDATE_STRATEGY: Final[str] = os.getenv("update_strategy", "version")
    # similarity_threshold：内容相似度阈值（cosine），≥ 则判定为重复，默认 0.95。
    SIMILARITY_THRESHOLD: Final[float] = float(os.getenv("similarity_threshold", "0.95"))
    # name_similarity_threshold：文件名相似度阈值（SequenceMatcher ratio），≥ 则判定为同名，默认 0.95。
    NAME_SIMILARITY_THRESHOLD: Final[float] = float(os.getenv("name_similarity_threshold", "0.95"))
    # old_version_retention_days：被替换的旧版本保留天数（到期后定时清理硬删除），默认 1095（3 年）。
    OLD_VERSION_RETENTION_DAYS: Final[int] = int(os.getenv("old_version_retention_days", "1095"))

    # ================= 降级告警 =================
    # 被 core/degradation_alert.py 读取：降级事件写入 logs/degradation.log；
    # critical 级别额外 POST 到 webhook。
    # degradation_notify_enabled：降级记录总开关，默认 true。
    DEGRADE_NOTIFY_ENABLED: Final[bool] = os.getenv(
        "degradation_notify_enabled", "true"
    ).lower() in ("1", "true", "yes")
    # degradation_webhook_url：critical 事件推送地址（如飞书/企微机器人），默认空=不推送。
    DEGRADE_WEBHOOK_URL: Final[str] = os.getenv("degradation_webhook_url", "")

    # ================= 邮箱验证码（SMTP） =================
    # 被 core/mailer.py 读取；app/application/auth/user.py 调用 mailer 发送登录验证码。
    # smtp_host：SMTP 服务器地址，留空则降级为日志输出验证码（仅开发环境）。
    SMTP_HOST: Final[str] = os.getenv("smtp_host", "")
    # smtp_port：端口，SSL 默认 465，STARTTLS 一般 587。
    SMTP_PORT: Final[int] = int(os.getenv("smtp_port", "465"))
    # smtp_user：发件邮箱账号，默认空。
    SMTP_USER: Final[str] = os.getenv("smtp_user", "")
    # smtp_password：【密钥类字段】邮箱授权码（非登录密码），只能来自环境变量或
    # env/config.env 本地文件，禁止入库、禁止硬编码；默认空。
    SMTP_PASSWORD: Final[str] = os.getenv("smtp_password", "")
    # smtp_use_ssl：true=SSL(465) 直连，false=STARTTLS(587) 升级，默认 true。
    SMTP_USE_SSL: Final[bool] = os.getenv("smtp_use_ssl", "true").lower() in (
        "1", "true", "yes",
    )
    # smtp_sender_name：发件人显示名称，默认「智能课程咨询服务」。
    SMTP_SENDER_NAME: Final[str] = os.getenv("smtp_sender_name", "智能课程咨询服务")

    # ================= Agent 策略参数 =================
    # 被 multi_agent 子包读取（state_machine.py、各 *_agent.py、failure_diagnoser.py、
    # verifier.py）及 app/application/chat/agent_service.py（重试编排与指标上报）。
    # 各 Agent 最大重试次数（失败后重试，超限进入兜底）；
    # 环境变量同名小写，默认值见各行。
    AGENT_MAX_RETRIES_VAGUE: Final[int] = int(os.getenv("agent_max_retries_vague", "2"))
    AGENT_MAX_RETRIES_ANALYSIS: Final[int] = int(os.getenv("agent_max_retries_analysis", "3"))
    AGENT_MAX_RETRIES_RETRIEVAL: Final[int] = int(os.getenv("agent_max_retries_retrieval", "3"))
    AGENT_MAX_RETRIES_SUMMARY: Final[int] = int(os.getenv("agent_max_retries_summary", "3"))
    AGENT_MAX_RETRIES_ROLLBACK: Final[int] = int(os.getenv("agent_max_retries_rollback", "3"))
    # 单次请求全局步数上限（防止无限重试/回退循环）
    AGENT_MAX_TOTAL_STEPS: Final[int] = int(os.getenv("agent_max_total_steps", "10"))
    # 循环检测滑动窗口大小
    AGENT_LOOP_WINDOW: Final[int] = int(os.getenv("agent_loop_window", "5"))
    # SummaryAgent 相关性回退：最大轮数 + 通过阈值
    AGENT_SUMMARY_MAX_ROUNDS: Final[int] = int(os.getenv("agent_summary_max_rounds", "3"))
    AGENT_SUMMARY_RELEVANCE_THRESHOLD: Final[float] = float(
        os.getenv("agent_summary_relevance_threshold", "0.6")
    )
    # FailureDiagnoser / IntentVerifier LLM 调用参数
    AGENT_DIAGNOSER_TIMEOUT: Final[int] = int(os.getenv("agent_diagnoser_timeout", "5"))
    AGENT_VERIFIER_TIMEOUT: Final[int] = int(os.getenv("agent_verifier_timeout", "5"))
    AGENT_VERIFIER_TEMPERATURE: Final[float] = float(
        os.getenv("agent_verifier_temperature", "0.1")
    )

    # ================= 工具调用层（function calling） =================
    # 被 tools 子包读取（protocol.py 的 ToolSpec 边界、dispatcher.py 的调度与超时、
    # function_app.domain.tools.py 的具体工具）。
    # 每个决策层单轮最多执行的工具调用数（防 LLM 无界调用）
    TOOL_MAX_CALLS_PER_TURN: Final[int] = int(os.getenv("tool_max_calls_per_turn", "5"))
    # 检索工具参数边界
    TOOL_TOP_K_MAX: Final[int] = int(os.getenv("tool_top_k_max", "10"))
    TOOL_QUERY_MAX_CHARS: Final[int] = int(os.getenv("tool_query_max_chars", "200"))
    # 工具执行默认超时（秒）；ToolSpec 可按工具覆盖
    TOOL_DEFAULT_TIMEOUT_SECONDS: Final[float] = float(
        os.getenv("tool_default_timeout_seconds", "20")
    )

    @property
    def CORS_ORIGINS_LIST(self) -> List[str]:
        """把逗号分隔的 CORS_ORIGINS 拆成去空白后的来源列表；被 control/app.py 的 CORSMiddleware 读取。"""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # storage_dir：运行时有状态数据根目录（相对 BASE_DIR 或绝对路径），默认 "storage"。
    # 上传文件与 Chroma 持久化统一收敛到此根下，与源码目录物理隔离（数据/源码分离）；
    # 可用环境变量 storage_dir 覆盖，绝对路径时直接使用。
    STORAGE_DIR_NAME: Final[str] = os.getenv("storage_dir", "storage")

    @property
    def STORAGE_DIR(self) -> Path:
        """运行时数据根目录绝对路径；STORAGE_DIR_NAME 为绝对路径时直接使用，否则拼到 BASE_DIR 下。"""
        p = Path(self.STORAGE_DIR_NAME)
        return p if p.is_absolute() else BASE_DIR / p

    # upload_dir：上传文件根目录（相对 BASE_DIR 或绝对路径），默认 storage/uploads。
    # 显式配置环境变量 upload_dir 时仍尊重旧值（向后兼容）。
    # 被 file_control / file_service / knowledge_service / temp_knowledge_store 等经 UPLOAD_DIR 读取。
    UPLOAD_DIR_NAME: Final[str] = os.getenv("upload_dir", "storage/uploads")

    @property
    def UPLOAD_DIR(self) -> Path:
        """上传目录的绝对路径：UPLOAD_DIR_NAME 为绝对路径时直接使用，否则拼到 BASE_DIR 下。"""
        p = Path(self.UPLOAD_DIR_NAME)
        return p if p.is_absolute() else BASE_DIR / p

    # chroma_dir：Chroma 持久化目录（相对 BASE_DIR 或绝对路径），默认 storage/chromadb。
    # 公共知识库向量库（app/infrastructure/vector_store/persistent）与文件演示库（app/domain/agents/file_agent）共用此定位；
    # 取代历史上散落在各模块的 ../chromadb_data/ 相对路径（CWD 依赖）。
    CHROMA_DIR_NAME: Final[str] = os.getenv("chroma_dir", "storage/chromadb")

    @property
    def CHROMA_DIR(self) -> Path:
        """Chroma 持久化目录绝对路径；CHROMA_DIR_NAME 为绝对路径时直接使用，否则拼到 BASE_DIR 下。"""
        p = Path(self.CHROMA_DIR_NAME)
        return p if p.is_absolute() else BASE_DIR / p

    # frontend_dist：前端生产构建产物目录（相对 BASE_DIR 或绝对路径），默认 web/frontend/dist。
    # 目录存在时 FastAPI 托管 SPA（/ 返回 index.html，静态资源走 /assets）；
    # 设为空字符串或不存在的路径则退化为纯 API 服务（/ 返回探活 JSON）。
    # 被 control/app.py 的静态托管/catch-all 逻辑经 FRONTEND_DIST_DIR 读取。
    FRONTEND_DIST: Final[str] = os.getenv("frontend_dist", "web/frontend/dist")

    @property
    def FRONTEND_DIST_DIR(self) -> Path:
        """前端产物目录绝对路径；FRONTEND_DIST 为空时返回空 Path（调用方据此判断退化为纯 API）。"""
        if not self.FRONTEND_DIST:
            return Path()
        p = Path(self.FRONTEND_DIST)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def UPLOAD_ALLOWED_EXT_SET(self) -> set:
        """把逗号分隔的允许扩展名串转为小写集合，供上传扩展名校验（file_control.py）做 O(1) 包含判断。"""
        return {e.strip().lower() for e in self.UPLOAD_ALLOWED_EXT.split(",") if e.strip()}

    @property
    def UPLOAD_MAX_BYTES(self) -> int:
        """上传大小上限（字节），由 UPLOAD_MAX_MB 换算；file_control.py 据此返回 413。"""
        return self.UPLOAD_MAX_MB * 1024 * 1024

    @property
    def IS_PROD(self) -> bool:
        """是否生产环境（APP_ENV 小写等于 prod）；被 app.py 启动校验与 LOG_JSON 读取。"""
        return self.APP_ENV.lower() == "prod"

    @property
    def LOG_JSON(self) -> bool:
        """日志是否采用 JSON：auto（默认）时跟随 IS_PROD，否则按 1/true/yes 解析；被 control/app.py 读取。"""
        if self.LOG_JSON_RAW.lower() == "auto":
            return self.IS_PROD
        return self.LOG_JSON_RAW.lower() in ("1", "true", "yes")


# 模块级全局单例：全仓唯一的配置实例，导入本模块时完成实例化（缺失 jwt_secret 会在此刻抛错阻断启动）。
settings = Settings()
