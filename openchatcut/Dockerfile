FROM node:22-alpine

RUN apk add --no-cache git ffmpeg python3 make g++ bash

RUN git clone --depth 1 https://github.com/0xsline/OpenChatCut.git /app
WORKDIR /app

ENV ONNXRUNTIME_NODE_INSTALL=skip
RUN npm install --ignore-engines

RUN cp .env.example .env.local

RUN npm run sync:mediapipe || true
RUN npm run verify:server-tool-catalog || true

RUN printf 'import base from "./vite.config.ts";\nimport { defineConfig, mergeConfig } from "vite";\nexport default defineConfig((env) => {\n  const b = typeof base === "function" ? base(env) : base;\n  return mergeConfig(b, { server: { watch: { ignored: ["**/.env.local", "**/.env*"] }, allowedHosts: ["openchatcut", "creation-openchatcut", "localhost", "127.0.0.1"] } });\n});\n' > config/vite.docker.config.ts

EXPOSE 5199
CMD ["npx", "vite", "--config", "config/vite.docker.config.ts", "--host", "0.0.0.0"]
