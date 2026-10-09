# 智能创作助手 · 单镜像多阶段构建
# 构建:  docker build -t creation-app:latest .
# 运行:  docker compose --profile app up -d     (app/editor/caddy 三个服务共用本镜像)
#
# 阶段 1:前端构建(node)→ dist
# 阶段 2:后端(python:3.12-slim + ffmpeg + 中文字体 + headless chromium)→ 直接跑两个服务

FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build                      # 产物:/web/dist

FROM python:3.12-slim

# ffmpeg/ffprobe:渲染与素材元数据;fonts-noto-cjk:字幕渲染的 CJK 字体
#
# 装三遍:个别 .deb 每轮都会被网络路丢包(502/500,每次失败的还不一样),单遍必炸
# exit 100;失败的字节留在 apt 缓存里,后一遍只补没取到的那几个——实测第二遍就过。
# 结尾的 ffmpeg/fc-list 是硬闸:装没装真上,层自己说话,不留「构建绿了但没有字体」。
RUN apt-get update -o Acquire::Retries=3 \
    && { apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
         || apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
         || apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk; } \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1 && fc-list :lang=zh | head -1

WORKDIR /app
COPY backend/requirements.txt backend/requirements-storyline.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-storyline.txt

# chromium:图形科普片(motion)与网页出片(render_web)都靠 headless 逐帧截图。
# 放在 pip 层之后——它同样吃网络丢包,所以同样装三遍(理由见上面 ffmpeg 层),
# 也别让它的前面那层(pip)因为改上面而被拖着重跑。
RUN apt-get update -o Acquire::Retries=3 \
    && { apt-get install -y --no-install-recommends chromium \
         || apt-get install -y --no-install-recommends chromium \
         || apt-get install -y --no-install-recommends chromium; } \
    && rm -rf /var/lib/apt/lists/* && chromium --version

COPY backend/ ./
# 前端产物放进 REPO_ROOT/frontend/dist(run_server 的静态目录锚点在容器内=/frontend/dist)
COPY --from=web /web/dist /frontend/dist

# 可选:构建期预置 whisper 模型(约 460MB,预置后首次 ASR 不用现下载)
#   docker build --build-arg WHISPER_PRELOAD=1 -t creation-app:latest .
ARG WHISPER_PRELOAD=0
RUN if [ "$WHISPER_PRELOAD" = "1" ]; then \
        python -c "from faster_whisper import WhisperModel; WhisperModel('small')"; \
    fi

EXPOSE 8000 8001
# 默认命令 = 主服务;剪辑服务由 compose 以 command 覆写共用同一镜像
CMD ["python", "run_server.py", "--port", "8000"]
