# syntax=docker/dockerfile:1
ARG NODE_TAG=24-alpine

FROM node:${NODE_TAG} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:${NODE_TAG} AS runtime
# Talks to the bind-mounted host socket, so it needs the docker client only.
RUN apk add --no-cache docker-cli
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist

# BROKER_CONFIG may point elsewhere, but never at a workspace-writable path.
COPY broker.config.yaml /etc/swarmforge/broker.config.yaml
ENV BROKER_CONFIG=/etc/swarmforge/broker.config.yaml
ENV PORT=8080
EXPOSE 8080

CMD ["node", "dist/src/index.js"]
