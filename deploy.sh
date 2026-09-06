#!/bin/bash
set -e

echo "📦 拉取最新代码..."
git pull

echo "🔨 构建 Docker 镜像..."
docker build -t xhs-mcp .

echo "🔄 重启容器..."
docker stop xhs-mcp 2>/dev/null || true
docker rm xhs-mcp 2>/dev/null || true
docker run -d --name xhs-mcp \
  -p 8000:8000 \
  -e MCP_AUTH_TOKEN="${MCP_AUTH_TOKEN:-20604002xhsmcp}" \
  --restart unless-stopped \
  xhs-mcp

echo "✅ 部署完成！"
docker logs --tail 5 xhs-mcp
