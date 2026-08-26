# amd64 only — see .github/workflows/docker.yml.
FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# No PUBLIC_ORIGIN build arg on purpose. This image is published to GHCR and
# pulled by deployments on hostnames it cannot know, so the Open Graph origin
# is applied at container start instead — see docker-entrypoint.d/40-og-origin.sh.
RUN npm run build && npm run check:no-cdn

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# Pristine copy the entrypoint rewrites from, kept outside the web root so it
# is never served. Rewriting from this rather than in place is what makes
# changing PUBLIC_ORIGIN and restarting work.
RUN mkdir -p /usr/share/pwa-yt \
    && cp /usr/share/nginx/html/index.html /usr/share/pwa-yt/index.html.tmpl
COPY nginx.conf /etc/nginx/conf.d/default.conf
# nginx's own entrypoint runs /docker-entrypoint.d/*.sh before starting; 40
# puts this after the stock 10–30 scripts. chmod because a Windows checkout
# carries no executable bit.
COPY docker-entrypoint.d/40-og-origin.sh /docker-entrypoint.d/40-og-origin.sh
RUN chmod +x /docker-entrypoint.d/40-og-origin.sh
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
