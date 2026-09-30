# OpsPilot Frontend (nginx 静态托管 + /api 反代)
FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable
# pnpm≥10 默认拦截依赖 postinstall；esbuild 必须执行构建脚本
RUN pnpm config set dangerouslyAllowAllBuilds true
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
