FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制服务端代码（作为 server 包）
COPY server/ ./server/

# 数据目录（运行时由环境变量 DATA_DIR 指定为 /app/data）
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 58000

# 以包方式运行，支持模块化解耦（from .state import ...）
CMD ["python", "-m", "server.app"]
