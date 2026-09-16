FROM python:3.11-slim

# 环境变量：禁止 Python 输出缓冲，保证 docker logs 实时看到日志
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 1. 安装基础系统依赖（CA 证书、时区数据）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. 安装 Python 核心依赖（OCI SDK、带有定时任务 JobQueue 的 TG Bot SDK）
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir oci "python-telegram-bot[job-queue]"

# 3. 复制代码到容器
COPY oci_bot.py /app/

# 4. 容器启动入口
CMD ["python3", "oci_bot.py"]