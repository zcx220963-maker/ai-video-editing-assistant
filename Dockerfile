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
# 母版（TSX）源码里被离线对账比的那几个文件也带一份：tests/test_code_spec.py ⑤ 拿
# Python 契约比 tokens.ts 的字幕档位与画幅、比 lint-rules.mjs 的 import 白名单、比
# package.json 的依赖——不在这里 COPY，这一条在 sanctioned 环境（creation-app 内跑套件）
# 就是读不到文件。拷贝只会因「改了 TSX 没重烤 app 镜像」而变旧，变旧报红不静默过。
COPY remotion/src/tokens.ts remotion/src/FilmLayout.tsx /app/remotion/src/
# 闸⑪ 要把代码里的 `palettes["deep-ocean"].text` 解析成真实色号才能算对比度：没有这份
# 色板，凡是从 palette 取色的镜只能记成「未判」，那一条就只剩字面十六进制颜色能判。
COPY remotion/src/themes/palettes.json /app/remotion/src/themes/palettes.json
# server.mjs 与 public/assets/ 也带一份（R1-T5/T4）：它们不参与后端运行，但
# `tests/test_delivery_hygiene.py` 的「孤儿判据」要先认「这是一棵完整源码树」
# （`src/FilmLayout.tsx` + `server.mjs` 两个哨兵）才肯判——只 COPY 一半，那几条
# 会记成「未验证」。未验证在一处**合理**（本地没这棵树），但 sanctioned 环境
# （就是本镜像）本该能判却判不了，于是「styles.json 还在不在」这类问题永远没人回答。
# 两个文件加起来 56 KB，比一次人肉体检便宜。
# `assetgate.mjs` 与 `imagecheck.mjs` 是 `server.mjs` 的 import：COPY 过来一份
# server.mjs 而不带它们，镜像里就躺着一段**连 import 都过不了**的 server.mjs，
# 而它正是「这是一棵完整源码树」的哨兵之一。tsconfig.json 带上是给 T9 的
# `@dg/themes/*` → `src/themes/*` 那一条在 sanctioned 环境里判得动（纯文本比对）。
COPY remotion/lint-rules.mjs remotion/package.json remotion/server.mjs \
     remotion/assetgate.mjs remotion/imagecheck.mjs remotion/tsconfig.json /app/remotion/
COPY remotion/public/assets/.gitkeep /app/remotion/public/assets/
# 仓库根那份 `scripts/`（素材摄取/巡检那几件）也带一份，**落在 `/scripts`**——不是
# `/app/scripts`（那是 backend 自己的脚本目录，两份同名文件会互相盖住）。
# 理由是同一件事：`tests/test_asset_sources.py` 与 `tests/test_asset_gc_report.py`
# 的被测对象就是这几个脚本，而镜像里 `WORKDIR /app` 就等于 backend——宿主布局
# 「<仓库根>/backend + <仓库根>/scripts」在容器里只剩前半截，这两份用例于是以
# `ModuleNotFoundError: asset_common` 的样子红（不像代码坏了，像环境不对，最容易
# 被当成「容器里跑不了，跳过」）。`/scripts` 让 `Path(__file__).resolve().parents[2]`
# 这条锚点在两个布局里都指得对：宿主是 <仓库根>，容器里就是 `/`。
# 这几个模块只连标准库 + `agent_framework.storage`（后者已在 /app），纯离线，
# 全套 104 KB。带一份的副作用只有一个：改了根 `scripts/` 没重烤镜像，这一条会变旧——
# 变旧报红，不静默过。
COPY scripts/ /scripts/
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
