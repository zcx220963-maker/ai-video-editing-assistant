# creation-remotion · 图形科普片的代码渲染链
# 构建: docker build -t creation-remotion:latest ./remotion
# 运行: docker compose --profile app up -d      (与 creation-editor 同构的独立常驻服务)
#
# 为什么单独一个镜像：Node + Remotion 只服务图形链，崩了不该带走后端；
# 也让 creation-app 维持 2.62 GB 不变（见 spec §2.1）。

# 基镜像选 node:24-slim，因为它的全部图层本机已经有（`docker images` 里挂着，构建零下载）。
# 换任何一个本机没有的 tag 都要现场拉：实测 `node:20-bookworm-slim` 走 ECR 的
# docker-library 源，47 分钟只把一层挪到 27 MB / 41 MB 就再也不动——本机 Docker 对
#  registry 的连接就是这样，与 compose 里 minio 走 quay.io、redis 走 public.ecr.aws 同源。
# 网络顺的那天想换回 20：把下面这行改成 `node:20-bookworm-slim` 即可，两者都是
# Debian bookworm，apt 层与 npm 层不用动。
FROM node:24-slim

# chromium: Remotion 逐帧 seek 的执行体（渲染时按 browserExecutable 参数指过来，
#            不让构建期再下一份 headless shell）
# fonts-noto-cjk: 中文字面。母版字体栈只认镜像里真装着的字族——列一个没装的名字
#            等于静默回落宋体，用户在界面上选的字体和画面上出现的不是一回事。
# ffmpeg/ffprobe: 渲完自己回读帧数与时长（闸③ 的原料），不等字节回到 Python 侧才发现对不上。
#
# 装三遍：个别 .deb 每轮都会被网络路丢包（502/500，每次失败的还不一样），单遍必炸
# exit 100；失败的字节留在 apt 缓存里，后一遍只补没取到的那几个。层末硬验是同一套纪律。
# fontconfig: 提供 fc-list —— 层末要用它证明中文字面真在镜像里，而 slim 基镜像本身不带它。
RUN apt-get update -o Acquire::Retries=3 \
    && { apt-get install -y --no-install-recommends chromium fonts-noto-cjk ffmpeg fontconfig \
         || apt-get install -y --no-install-recommends chromium fonts-noto-cjk ffmpeg fontconfig \
         || apt-get install -y --no-install-recommends chromium fonts-noto-cjk ffmpeg fontconfig; } \
    && rm -rf /var/lib/apt/lists/* \
    && chromium --version && ffprobe -version | head -1 && fc-list :lang=zh | head -1

WORKDIR /srv

# npm ci 而不是 npm install：lockfile 哈希进 cache_key，所以「同输入同像素」
# 一半靠它。install 会顺手升版本，等于每次构建都在换像素。
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# 层末硬验：三个口的那份文件解析得动（注释里只要出现 `*/` 就会提前关掉块注释，
# 这类错误镜像能烤成功、运行时才炸，所以必须在构建期拦）；闸① 真能加载
# （ESLint 配置与自定义规则都在这一层）；母版文件真在。
#
# public/assets/ 那一验（R1-T4）：`.dockerignore` 里 `public/assets/*` 把素材字节挡在
# 上下文外，只留 `.gitkeep` 这个脚手架——两行规则谁写漏一行，`COPY . .` 都会静静地
# 什么都不带，镜像照样烤成功，而 `/stage-assets` 到运行时才发现没有落脚点。
# 所以「目录真在」由构建层自己说话，不靠人记得。
#
# 白名单 ↔ node_modules 这一条也在构建期：ALLOWED_IMPORTS 是「模型可以写哪些 import」，
# package.json 是「镜像里真装了哪些包」，两份名单各写一遍、不在同一个进程里。
# 白名单多出一个名字时，构建照样成功、lint 照样放行——炸的是打包期那句「解析失败」，
# 而收到它的是照着白名单写代码的模型，看到的是一行跟它写的 import 长得不一样的错。
# 烤不成功的镜像才是会报警的那一个。
#
# 这一验用 `import.meta.resolve` 而不是 `createRequire(...).resolve`：后者按 **require**
# 条件解析，而 `@remotion/rough-notation` 这类包的 `exports` 只给了 `import` 条件
# （`{".": {"types":…, "module":…, "import":…}}`，没有 `require`）——包**装着**，
# 按要求解析却报 ERR_PACKAGE_PATH_NOT_EXPORTED，构建因此红在一个假故障上。
# 我们要判的是「模型照白名单写的 `import` 在打包期解不解得开」，那就得按 import 条件问。
RUN node -e "const{execSync}=require('child_process');const fs=require('fs');const out=execSync('fc-list : family',{encoding:'utf8'});const fams=[...new Set(out.split('\n').flatMap(l=>l.split(',').map(s=>s.trim())).filter(Boolean))].sort();fs.writeFileSync('font-whitelist.json',JSON.stringify(fams,null,2));const need=['Noto Sans CJK SC','Noto Serif CJK SC','Noto Sans Mono CJK SC'];const miss=need.filter(f=>!fams.includes(f));if(miss.length)throw new Error('镜像缺字体：'+miss.join(', '));console.log('字体白名单',fams.length,'族')" \
    && node --check server.mjs \
    && node -e "import('./lint.mjs').then(async (m) => { const r = await m.lintShotCode('x'); if (r.ok) throw new Error('空代码竟然过了 lint'); console.log('lint 闸就绪，空代码报', r.errors.length, '条'); })" \
    && node -e "const p=require('./package.json'); if (!p.dependencies.remotion) throw new Error('remotion 没进依赖'); console.log('remotion', p.dependencies.remotion)" \
    && node --input-type=module -e "const m = await import('./lint-rules.mjs'); const bad = []; for (const p of m.ALLOWED_IMPORTS) { if (p.startsWith('@dg/')) continue; try { import.meta.resolve(p); } catch (e) { bad.push(p + '（' + (e.code || e.message) + '）'); } } if (bad.length) throw new Error('import 白名单里的包镜像里没装：' + bad.join('、') + '——要么装进 package.json，要么从白名单删掉'); console.log('import 白名单', m.ALLOWED_IMPORTS.size, '项全部可解析')" \
    && test -f src/FilmLayout.tsx && test -f src/index.ts \
    && test -d public/assets \
    && test -d public/fonts \
    && node tests/gate1-lint.mjs \
    && node tests/gate13-assets.mjs \
    && node tests/shipped-namespace.mjs

# 闸⑬ 的三张红样（1×1 / 全白 / manifest 有记录但文件缺）也在构建期跑：判据里的
# 阈值（纯色占比、主色档数）是可调的旋钮，调坏了不会让任何功能挂掉，只会让闸
# 永远放行——那种失效在任何一次真渲染里都看不出来，只有红样会说话。测试把
# public/ 指到临时目录（DG_PUBLIC_ROOT），不碰镜像里的 public/assets/。

# DG_GL 是实测出来的，不是猜的（容器内 Chromium 154 + 一段 three.js 四棱锥探针，
# 见 tests/gl-probe.mjs）：angle / egl / swiftshader 三档全部
# "THREE.WebGLRenderer: Error creating WebGL context."，只有 swangle 出得来带光影的
# 几何体与正确遮挡。这一档定生死——选错等于整条 3D 通道没有。
ENV PORT=8010 DG_GL=swangle DG_BROWSER=/usr/bin/chromium
EXPOSE 8010
CMD ["node", "server.mjs"]
