# syntax=docker/dockerfile:1
ARG NODE_TAG=24-alpine

# --- build: compile TypeScript to dist/ with the full dev toolchain ----------
FROM node:${NODE_TAG} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# --- runtime: docker CLI + production deps + compiled output -----------------
FROM node:${NODE_TAG} AS runtime
# The broker spawns worker containers by talking to the host docker socket
# (bind-mounted in by the launcher), so it needs the docker *client* only.
RUN apk add --no-cache docker-cli
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist

# Baked command set. Override BROKER_CONFIG to point at a different (trusted,
# never workspace-writable) path if you mount one instead.
COPY broker.config.yaml /etc/swarmforge/broker.config.yaml
ENV BROKER_CONFIG=/etc/swarmforge/broker.config.yaml
ENV PORT=8080
EXPOSE 8080

CMD ["node", "dist/src/index.js"]
