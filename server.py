"""
小红书 MCP Server —— 让 AI 看到小红书笔记（图文 + 视频抽帧）

用法：
  本地测试:  python server.py
  部署时设环境变量: MCP_AUTH_TOKEN=你的密码 python server.py
  然后通过 claude.ai → Add custom connector 接入
"""

import json
import hashlib
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, parse_qs, urlparse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
import uvicorn

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

# ============================================================
#  配置
# ============================================================

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "xhs-mcp-2024")

# OAuth 临时存储（单进程足够）
_auth_codes: dict[str, dict] = {}   # code -> {client_id, redirect_uri, expires}
_access_tokens: set[str] = set()
_registered_clients: dict[str, dict] = {}  # client_id -> {client_secret, redirect_uris}

# ============================================================
#  FastMCP 服务器（业务逻辑）
# ============================================================

mcp = FastMCP(
    "小红书笔记助手",
    instructions="帮 AI 看小红书笔记：图文 + 视频抽帧",
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

MAX_VIDEO_SIZE = 200 * 1024 * 1024
CACHE: dict[str, tuple[float, list]] = {}
CACHE_TTL = 6 * 3600


def _get_cached(url: str):
    if url in CACHE:
        ts, result = CACHE[url]
        if time.time() - ts < CACHE_TTL:
            return result
        del CACHE[url]
    return None


def _set_cache(url: str, result: list):
    CACHE[url] = (time.time(), result)


async def _resolve_short_link(client: httpx.AsyncClient, url: str) -> str:
    if "xhslink.com" in url:
        resp = await client.get(url, headers=HEADERS, follow_redirects=True)
        return str(resp.url)
    return url


def _parse_initial_state(html: str) -> dict | None:
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.+?})\s*</script>", html, re.DOTALL)
    if not match:
        return None
    raw = match.group(1)
    raw = re.sub(r'\bundefined\b', 'null', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_note_data(state: dict) -> dict | None:
    try:
        return state["note"]["noteDetailMap"]
    except (KeyError, TypeError):
        pass
    try:
        return state["noteData"]["data"]["noteData"]
    except (KeyError, TypeError):
        pass
    try:
        return state["normalNotePreloadData"]["noteData"]
    except (KeyError, TypeError):
        pass
    return None


def _get_first_note(detail_map: dict) -> dict | None:
    if isinstance(detail_map, dict):
        for key in detail_map:
            note = detail_map[key]
            if isinstance(note, dict) and "note" in note:
                return note["note"]
            return note
    return detail_map if isinstance(detail_map, dict) else None


def _extract_images(note: dict) -> list[str]:
    urls = []
    image_list = note.get("imageList") or []
    for img in image_list:
        info_list = img.get("infoList") or []
        best_url = None
        for info in info_list:
            if info.get("imageScene") == "WB_DFT":
                best_url = info.get("url")
                break
        if not best_url and info_list:
            best_url = info_list[-1].get("url")
        if not best_url:
            best_url = img.get("urlDefault") or img.get("url")
        if best_url:
            if best_url.startswith("//"):
                best_url = "https:" + best_url
            urls.append(best_url)
    return urls


def _extract_video_url(note: dict) -> str | None:
    video = note.get("video")
    if not video:
        return None
    try:
        streams = video["media"]["stream"]
        for codec in ("h264", "h265", "av1"):
            codec_streams = streams.get(codec, [])
            if codec_streams:
                url = codec_streams[0].get("masterUrl")
                if url:
                    return url
                backups = codec_streams[0].get("backupUrls", [])
                if backups:
                    return backups[0]
    except (KeyError, TypeError, IndexError):
        pass
    try:
        key = video["consumer"]["originVideoKey"]
        if key:
            return f"https://sns-video-bd.xhscdn.com/{key}"
    except (KeyError, TypeError):
        pass
    return video.get("url")


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, headers={"User-Agent": MOBILE_UA}, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


async def _download_video_and_extract_frames(
    client: httpx.AsyncClient, video_url: str
) -> list[bytes]:
    frames = []
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "video.mp4"
        try:
            async with client.stream(
                "GET", video_url, headers={"User-Agent": MOBILE_UA}, timeout=120,
            ) as resp:
                if resp.status_code != 200:
                    return frames
                downloaded = 0
                with open(video_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(8192):
                        downloaded += len(chunk)
                        if downloaded > MAX_VIDEO_SIZE:
                            return frames
                        f.write(chunk)
        except Exception:
            return frames
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, timeout=30,
            )
            duration = float(result.stdout.strip())
        except Exception:
            return frames
        n_frames = max(4, min(8, int(duration / 8)))
        fps_val = n_frames / duration if duration > 0 else 1
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(video_path),
                 "-vf", f"fps={fps_val},scale='min(960,iw)':-2",
                 "-q:v", "4", str(Path(tmpdir) / "frame-%02d.jpg")],
                capture_output=True, timeout=120,
            )
        except Exception:
            return frames
        frame_files = sorted(Path(tmpdir).glob("frame-*.jpg"))
        for fp in frame_files:
            frames.append(fp.read_bytes())
    return frames


def _format_note_text(note: dict) -> str:
    title = note.get("title", "")
    desc = note.get("desc", "")
    user = note.get("user", {})
    nickname = user.get("nickname", "未知作者")
    interact = note.get("interactInfo", {})
    liked = interact.get("likedCount", "")
    collected = interact.get("collectedCount", "")
    comment_count = interact.get("commentCount", "")
    note_type = note.get("type", "")
    type_label = "视频笔记" if note_type == "video" else "图文笔记"
    lines = [f"📝 【{type_label}】{title}"]
    lines.append(f"✍️ 作者：{nickname}")
    if desc:
        lines.append(f"\n{desc}")
    stats = []
    if liked:
        stats.append(f"❤️ {liked}")
    if collected:
        stats.append(f"⭐ {collected}")
    if comment_count:
        stats.append(f"💬 {comment_count}")
    if stats:
        lines.append("\n" + "  ".join(stats))
    return "\n".join(lines)


@mcp.tool()
async def xhs_peek(url: str, image_mode: str = "inline") -> list:
    """
    查看小红书笔记：输入分享链接，返回笔记文字 + 配图/视频抽帧。

    参数:
        url: 小红书笔记分享链接（短链 xhslink.com 或长链均可）
        image_mode: "inline" 返回图片内容块（默认），"url" 只返回图片链接列表
    """
    cached = _get_cached(url)
    if cached:
        return cached

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        real_url = await _resolve_short_link(client, url)
        resp = await client.get(real_url, headers=HEADERS)
        if resp.status_code != 200:
            return [f"❌ 请求失败，状态码 {resp.status_code}。请确认链接有效。"]
        html = resp.text
        state = _parse_initial_state(html)
        if not state:
            return ["❌ 未能从页面中提取数据。可能页面结构已变更，或链接需要 token。建议用 app 内「复制链接」获取的短链重试。"]
        detail_map = _extract_note_data(state)
        if not detail_map:
            return ["❌ 未找到笔记数据。页面结构可能已更新。"]
        note = _get_first_note(detail_map)
        if not note:
            return ["❌ 无法解析笔记内容。"]
        result = []
        text = _format_note_text(note)
        result.append(text)
        is_video = note.get("type") == "video"
        if image_mode == "url":
            image_urls = _extract_images(note)
            if image_urls:
                result.append("\n🖼️ 配图链接：\n" + "\n".join(
                    f"  [{i+1}] {u}" for i, u in enumerate(image_urls)
                ))
            if is_video:
                video_url = _extract_video_url(note)
                if video_url:
                    result.append(f"\n🎬 视频直链：{video_url}")
                    result.append("（提示：可用 ffmpeg 抽帧查看视频内容）")
        else:
            image_urls = _extract_images(note)
            if image_urls and not is_video:
                result.append(f"\n🖼️ 共 {len(image_urls)} 张配图：")
                for i, img_url in enumerate(image_urls):
                    img_data = await _download_image(client, img_url)
                    if img_data:
                        result.append(Image(data=img_data, media_type="image/jpeg"))
                    else:
                        result.append(f"  [图{i+1}] 下载失败：{img_url}")
            if is_video:
                video_url = _extract_video_url(note)
                if video_url:
                    result.append("\n🎬 这是一条视频笔记，正在抽帧生成连环画...")
                    frames = await _download_video_and_extract_frames(client, video_url)
                    if frames:
                        result.append(f"📽️ 视频抽帧（共 {len(frames)} 帧，按时间顺序排列）：")
                        for i, frame_data in enumerate(frames):
                            result.append(Image(data=frame_data, media_type="image/jpeg"))
                    else:
                        result.append("⚠️ 视频抽帧失败。可能是视频过大或 ffmpeg 不可用。")
                        if image_urls:
                            result.append("📸 降级为封面图：")
                            cover_data = await _download_image(client, image_urls[0])
                            if cover_data:
                                result.append(Image(data=cover_data, media_type="image/jpeg"))
                else:
                    result.append("⚠️ 未能提取视频链接。")
        comments = note.get("comments") or []
        if comments:
            result.append("\n💬 热门评论：")
            for c in comments[:5]:
                c_user = c.get("userInfo", {}).get("nickname", "匿名")
                c_content = c.get("content", "")
                if c_content:
                    result.append(f"  · {c_user}：{c_content}")
        _set_cache(url, result)
        return result


# ============================================================
#  OAuth 2.0 端点（给 claude.ai 连接用）
# ============================================================

def _get_base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


async def oauth_metadata(request: Request):
    base = _get_base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
    })


async def register_client(request: Request):
    body = await request.json()
    client_id = secrets.token_hex(16)
    client_secret = secrets.token_hex(32)
    redirect_uris = body.get("redirect_uris", [])
    _registered_clients[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "client_name": body.get("client_name", ""),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
    })


async def authorize(request: Request):
    params = dict(request.query_params)
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")

    if request.method == "GET":
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>小红书 MCP - 授权</title>
        <style>
            body {{ font-family: -apple-system, system-ui, sans-serif; display: flex;
                   justify-content: center; align-items: center; min-height: 100vh;
                   margin: 0; background: #f5f5f5; }}
            .card {{ background: white; padding: 32px; border-radius: 16px;
                     box-shadow: 0 4px 24px rgba(0,0,0,0.1); max-width: 360px; width: 90%; }}
            h2 {{ margin: 0 0 8px; font-size: 20px; }}
            p {{ color: #666; font-size: 14px; margin: 0 0 24px; }}
            input {{ width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px;
                     font-size: 16px; box-sizing: border-box; margin-bottom: 16px; }}
            button {{ width: 100%; padding: 12px; background: #e74c3c; color: white;
                      border: none; border-radius: 8px; font-size: 16px; cursor: pointer; }}
            button:hover {{ background: #c0392b; }}
        </style></head>
        <body><div class="card">
            <h2>📕 小红书 MCP 授权</h2>
            <p>输入你设置的密码来授权 AI 助手访问</p>
            <form method="POST">
                <input type="password" name="token" placeholder="请输入密码" required autofocus>
                <input type="hidden" name="client_id" value="{client_id}">
                <input type="hidden" name="redirect_uri" value="{redirect_uri}">
                <input type="hidden" name="state" value="{state}">
                <input type="hidden" name="code_challenge" value="{code_challenge}">
                <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
                <button type="submit">授权连接</button>
            </form>
        </div></body></html>
        """)

    # POST: 验证密码
    form = await request.form()
    token = form.get("token", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "")

    if token != AUTH_TOKEN:
        return HTMLResponse("""
        <!DOCTYPE html>
        <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>授权失败</title>
        <style>
            body { font-family: -apple-system, system-ui, sans-serif; display: flex;
                   justify-content: center; align-items: center; min-height: 100vh;
                   margin: 0; background: #f5f5f5; }
            .card { background: white; padding: 32px; border-radius: 16px;
                     box-shadow: 0 4px 24px rgba(0,0,0,0.1); text-align: center; }
        </style></head>
        <body><div class="card"><h2>❌ 密码错误</h2><p>请关闭页面后重试</p></div></body></html>
        """, status_code=403)

    code = secrets.token_hex(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "expires": time.time() + 300,
    }

    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}code={code}&state={state}",
        status_code=302,
    )


async def token_endpoint(request: Request):
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type")

    if grant_type == "authorization_code":
        code = body.get("code", "")
        code_verifier = body.get("code_verifier", "")

        if code not in _auth_codes:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        auth_code = _auth_codes.pop(code)
        if time.time() > auth_code["expires"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # PKCE 验证
        if auth_code.get("code_challenge"):
            expected = hashlib.sha256(code_verifier.encode()).digest()
            import base64
            expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
            if expected_b64 != auth_code["code_challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

        access_token = secrets.token_hex(32)
        refresh_token = secrets.token_hex(32)
        _access_tokens.add(access_token)

        return JSONResponse({
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": refresh_token,
        })

    elif grant_type == "refresh_token":
        access_token = secrets.token_hex(32)
        refresh_token = secrets.token_hex(32)
        _access_tokens.add(access_token)
        return JSONResponse({
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": refresh_token,
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def _check_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        return token in _access_tokens
    return False


# ============================================================
#  组合应用
# ============================================================

mcp_app = mcp.http_app(transport="streamable-http", path="/mcp")

async def protected_mcp(request: Request):
    if not _check_bearer(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # 转给 FastMCP 处理
    response = await mcp_app(request.scope, request.receive, request._send)
    return response


async def health(request: Request):
    return JSONResponse({"status": "ok", "name": "小红书笔记助手"})


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope.get("headers", []))
            auth = b""
            for key, value in scope.get("headers", []):
                if key == b"authorization":
                    auth = value
                    break
            if auth.startswith(b"Bearer "):
                token = auth[7:].decode()
                if token in _access_tokens:
                    await self.app(scope, receive, send)
                    return
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


oauth_routes = [
    Route("/.well-known/oauth-authorization-server", oauth_metadata),
    Route("/register", register_client, methods=["POST"]),
    Route("/authorize", authorize, methods=["GET", "POST"]),
    Route("/token", token_endpoint, methods=["POST"]),
    Route("/", health),
]

oauth_app = Starlette(routes=oauth_routes)


class CombinedApp:
    def __init__(self, oauth_app, mcp_app):
        self.oauth_app = oauth_app
        self.mcp_app = mcp_app
        self._lifespan_started = False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.mcp_app(scope, receive, send)
            return

        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            auth = b""
            for key, value in scope.get("headers", []):
                if key == b"authorization":
                    auth = value
                    break
            if auth.startswith(b"Bearer "):
                token = auth[7:].decode()
                if token in _access_tokens:
                    await self.mcp_app(scope, receive, send)
                    return
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.oauth_app(scope, receive, send)


app = CombinedApp(oauth_app, mcp_app)

if __name__ == "__main__":
    print(f"\n🔑 当前授权密码: {AUTH_TOKEN}")
    print(f"   (可通过环境变量 MCP_AUTH_TOKEN 修改)\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
