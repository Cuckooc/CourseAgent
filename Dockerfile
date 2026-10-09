# 后端 API 镜像（真实运行环境为 Python 3.8）
FROM python:3.8-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖以利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝代码（.dockerignore 排除运行时数据与本地密钥）
COPY . .

# 运行时数据目录（容器内由 compose 挂载持久卷，统一收敛到 /app/storage）
# storage/uploads：上传文件；storage/chromadb：Chroma 持久化
# 注意：file_analysis（源码包）与 data/（种子 JSON/样本）随 COPY . . 进镜像，勿排除
RUN mkdir -p storage/chromadb storage/uploads logs \
    && useradd -r -u 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 多 worker：WEB_CONCURRENCY 控制（默认 4），uvicorn 内置多进程管理器
# --proxy-headers：信任反向代理传来的真实 IP/协议（compose 内仅 nginx 可达）
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
      --workers ${WEB_CONCURRENCY:-4} --proxy-headers --forwarded-allow-ips='*'"]
