FROM node:22-alpine AS builder

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine AS runtime

ENV BACKEND_UPSTREAM=http://backend:8000
ENV SERVER_NAME=_
COPY --from=builder /build/dist/ /usr/share/nginx/html/
COPY deploy/nginx/http.conf.template /opt/keyword-nginx/http.conf.template
COPY deploy/nginx/https.conf.template /opt/keyword-nginx/https.conf.template
COPY deploy/nginx/40-configure-site.sh /docker-entrypoint.d/40-configure-site.sh
RUN chmod 0755 /docker-entrypoint.d/40-configure-site.sh

EXPOSE 80 443
