# StudyMatrix / 遗忘刺客 Agent —— 生产部署镜像
# 构建：  docker build -t studymatrix .
# 运行：  docker run -d -p 5000:5000 -e DEEPSEEK_API_KEY=sk-xxx -v sm_data:/app/data studymatrix
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码
COPY app.py render.yaml ./
COPY agents/ agents/
COPY tools/ tools/
COPY static/ static/
COPY data/ data/

# SQLite 数据目录（挂载卷持久化，防容器重建丢数据）
VOLUME ["/app/data"]

EXPOSE 5000

# gunicorn 生产启动（render.yaml 同款参数）
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
