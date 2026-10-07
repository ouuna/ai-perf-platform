# AI 性能测试平台镜像
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用层缓存）
COPY pyproject.toml README.md ./
COPY app ./app
COPY demo_target ./demo_target

RUN pip install --no-cache-dir .

EXPOSE 8000 8001

# 默认启动平台 Web 服务；靶站用单独的命令启动
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
