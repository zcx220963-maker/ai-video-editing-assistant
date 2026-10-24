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
# nodejs/npm：**外部技能的执行期**（2026-10-23）。用户导入了 7 个 Claude Code 风格
# 的技能（lemo-opuscar / mg-styles-15 / huashu-art-motion / xilo-opus-video …），
# 它们的 `.mjs` 脚本要 node 才能跑；容器里原先只有 python3/ffmpeg/chromium，
# 于是 `run` 工具放行了 node 也没有二进制。装在这一层而不是新开一层：同样会撞
# apt 网络抖动，合进去少一次 502/500 的风险。
RUN apt-get update -o Acquire::Retries=3 \
    && { apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk nodejs npm \
         || apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk nodejs npm \
         || apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk nodejs npm; } \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1 && fc-list :lang=zh | head -1 \
    && node --version && npm --version

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
# 「文字库」面板的字幕样张底稿：`code_spec.sample_shot_code()` 读它，渲预览图时
# 当画面底稿（母版照旧画字幕条）。它不参与镜像里的打包，但确实是一份镜头代码——
# 读不到时预览不出图（不报错、不影响出片），所以少 COPY 这一行的表现是「预览一直是空的」。
COPY remotion/src/preview/caption-sample.tsx /app/remotion/src/preview/caption-sample.tsx
# 内部组件目录（`storyline_server/motion/components.py`）是**从这份源码现读**的导出名与
# `*Props` 字段：不 COPY 它，容器里的目录就是空的——不报错，只是写手又回到「prop 名靠猜」，
# 猜错静默落缺省值、画面不对也不红。风格模板不在这棵树里（它们由 `style_templates` 单独注入）。
COPY remotion/src/lib /app/remotion/src/lib
# server.mjs 也带一份（R1-T5/T4）：它不参与后端运行，但
# `tests/test_delivery_hygiene.py` 的「孤儿判据」要先认「这是一棵完整源码树」
# （`src/FilmLayout.tsx` + `server.mjs` 两个哨兵）才肯判——只 COPY 一半，那几条
# 会记成「未验证」。未验证在一处**合理**（本地没这棵树），但 sanctioned 环境
# （就是本镜像）本该能判却判不了，于是「styles.json 还在不在」这类问题永远没人回答。
# 几十 KB，比一次人肉体检便宜。
# `lint.mjs` 与 `motionsample.mjs` 是 `server.mjs` 的 import（`lint.mjs` 自己再
# import `lint-rules.mjs`）：COPY 过来一份 server.mjs 而不带它们，镜像里就躺着
# 一段**连 import 都过不了**的 server.mjs，而它正是「这是一棵完整源码树」的哨兵
# 之一。tsconfig.json 带上是给 T9 的 `@dg/themes/*` → `src/themes/*` 那一条在
# sanctioned 环境里判得动（纯文本比对）。
# 2026-10-16：`assetgate.mjs` / `imagecheck.mjs` 随闸⑬ 与投递腿一起删了，
# `public/assets/.gitkeep` 那一条 COPY 也删了（供图通道撤掉后 `public/` 里
# 只剩随镜像发出去的 `fonts/`）。
COPY remotion/lint-rules.mjs remotion/lint.mjs remotion/motionsample.mjs \
     remotion/package.json remotion/server.mjs remotion/tsconfig.json /app/remotion/
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
