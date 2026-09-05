# 小红书 MCP Server

让 AI 看到小红书笔记——图文 + 视频抽帧。

## 功能

- `xhs_peek(url)` — 传入小红书分享链接，返回笔记文字 + 配图 / 视频抽帧
- 自动解析 `xhslink.com` 短链
- 视频笔记用 ffmpeg 抽帧（每 8 秒一帧，4~8 帧），让 AI 像看连环画一样"看"视频
- 6 小时内同一链接自动缓存，不重复抓取
- `image_mode="url"` 降级参数：只返回文字 + 图片链接（给不支持图片内容块的客户端用）

## 快速开始

### 本地运行

```bash
cd mcp-server
pip install -r requirements.txt
python server.py
# 服务跑在 http://localhost:8000/mcp
```

### Docker 部署

```bash
cd mcp-server
docker build -t xhs-mcp .
docker run -p 8000:8000 xhs-mcp
```

## 接入 claude.ai

### 方式一：Claude Code 本地连（最简单）

在项目根目录的 `.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "xhs": {
      "type": "url",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

然后在 Claude Code 里直接发小红书链接，它就能调用 `xhs_peek` 了。

### 方式二：claude.ai 官方客户端连（需部署到公网）

1. **部署服务**：把这个 MCP server 部署到任何能跑容器的平台（Railway / Render / Fly.io / 你自己的 VPS）
2. **加 OAuth shim**（claude.ai 要求连接器走 OAuth）：
   - 最简单的方式：用一个固定 token 做验证
   - 参考 [MCP OAuth 文档](https://modelcontextprotocol.io/docs/concepts/authentication)
3. **在 claude.ai 添加**：
   - 打开 claude.ai → Settings → Integrations → Add custom integration
   - 填入你的服务地址
   - 授权后即可使用

### 方式三：Claude Desktop 连

在 Claude Desktop 的配置文件中添加（Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "xhs": {
      "command": "python",
      "args": ["/你的路径/mcp-server/server.py"],
      "env": {}
    }
  }
}
```

> 注意：Desktop 走 stdio，server.py 默认启动 streamable-http。
> 如需 stdio 模式，改最后一行为 `mcp.run(transport="stdio")`

## 使用方式

连好之后，直接给 AI 发小红书链接就行：

```
帮我看看这篇笔记：https://xhslink.com/xxxxxx
```

AI 会自动调用 `xhs_peek`，看到完整的文字、图片和视频帧。

## 注意事项

- 请用 app 内「复制链接」拿到的短链（xhslink.com），最稳定
- 自用小工具，请自觉加节制：同一篇笔记不要频繁重复抓
- 页面结构偶尔会变，解析路径已做多候选兼容
- 视频抽帧需要 ffmpeg，没装时会降级为「封面 + 文字」
