# 单镜像多入口：Runtime（all / scheduler / worker）与一次性迁移共用本镜像。
# 构建：docker build -t fin-data-platform:local .
# 运行：见 docker-compose.yml（编排：数据库 + Redis + 迁移 + Runtime）
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FDP_ALEMBIC_INI=/app/alembic.ini \
    FDP_ALEMBIC_SCRIPT_LOCATION=/app/migrations

LABEL org.opencontainers.image.title="fin-data-platform" \
      org.opencontainers.image.description="金融数据平台（PIT 语义）：采集 / 存储 / 控制面 Runtime" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 先装依赖（利用层缓存）：包 + 全部数据源 extras；安装后清理构建源（单一代码副本）
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir \
        ".[platform,cache,api,derive,tushare,akshare,ifind,wind,fuyao,baostock]" \
    && rm -rf /app/build /app/src /app/pyproject.toml

# 迁移资产需随镜像携带（pip 不打包 alembic.ini / migrations）
COPY alembic.ini ./
COPY migrations ./migrations

# WebUI 静态资源（预构建：web/ 下 npm run build；服务经 FDP_WEB_DIST 托管）
COPY web/dist ./web/dist
ENV FDP_WEB_DIST=/app/web/dist

RUN useradd --create-home --shell /usr/sbin/nologin fdp \
    && chown -R fdp:fdp /app
USER fdp

# 健康检查：就绪检查（数据库 / 字典 / schema 版本），0 通过 / 1 未通过
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "fin_data_platform.runtime", "--check"]

CMD ["python", "-m", "fin_data_platform.runtime", "--role", "all"]
