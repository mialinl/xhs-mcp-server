"""
小红书 MCP Server —— 让 AI 看到小红书笔记（图文 + 视频抽帧）

用法：
  本地测试:  python server.py
  部署后通过 claude.ai → Add custom connector 接入
"""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

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

MAX_VIDEO_SIZE = 200 * 1024 * 1024  # 200MB
CACHE: dict[str, tuple[float, list]] = {}
CACHE_TTL = 6 * 3600  # 6 小时缓存


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
    """解析 xhslink.com 短链 → 实际笔记 URL"""
    if "xhslink.com" in url:
        resp = await client.get(url, headers=HEADERS, follow_redirects=True)
        return str(resp.url)
    return url


def _parse_initial_state(html: str) -> dict | None:
    """从 HTML 中提取 window.__INITIAL_STATE__ JSON 数据"""
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
    """从 __INITIAL_STATE__ 中提取笔记数据"""
    # 主路径
    try:
        return state["note"]["noteDetailMap"]
    except (KeyError, TypeError):
        pass
    # 备选路径
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
    """从 noteDetailMap 中取第一条笔记"""
    if isinstance(detail_map, dict):
        for key in detail_map:
            note = detail_map[key]
            if isinstance(note, dict) and "note" in note:
                return note["note"]
            return note
    return detail_map if isinstance(detail_map, dict) else None


def _extract_images(note: dict) -> list[str]:
    """提取笔记配图 URL（选最清晰的 WB_DFT 版本）"""
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
    """提取视频直链"""
    video = note.get("video")
    if not video:
        return None
    # 路径 1: media.stream.h264[0].masterUrl
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
    # 路径 2: consumer.originVideoKey
    try:
        key = video["consumer"]["originVideoKey"]
        if key:
            return f"https://sns-video-bd.xhscdn.com/{key}"
    except (KeyError, TypeError):
        pass
    # 路径 3: video.url (老结构)
    return video.get("url")


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    """下载配图（服务端下载，不带 Referer 绕防盗链）"""
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": MOBILE_UA},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


async def _download_video_and_extract_frames(
    client: httpx.AsyncClient, video_url: str
) -> list[bytes]:
    """下载视频 + ffmpeg 抽帧，返回 JPEG 帧列表"""
    frames = []
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "video.mp4"
        # 下载视频（限 200MB）
        try:
            async with client.stream(
                "GET",
                video_url,
                headers={"User-Agent": MOBILE_UA},
                timeout=120,
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

        # ffprobe 读时长
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    str(video_path),
                ],
                capture_output=True, text=True, timeout=30,
            )
            duration = float(result.stdout.strip())
        except Exception:
            return frames

        # 每 8 秒一帧，最少 4 帧、最多 8 帧
        n_frames = max(4, min(8, int(duration / 8)))
        fps_val = n_frames / duration if duration > 0 else 1

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", str(video_path),
                    "-vf", f"fps={fps_val},scale='min(960,iw)':-2",
                    "-q:v", "4",
                    str(Path(tmpdir) / "frame-%02d.jpg"),
                ],
                capture_output=True, timeout=120,
            )
        except Exception:
            return frames

        frame_files = sorted(Path(tmpdir).glob("frame-*.jpg"))
        for fp in frame_files:
            frames.append(fp.read_bytes())

    return frames


def _format_note_text(note: dict) -> str:
    """格式化笔记文字内容"""
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

        # 笔记文字
        text = _format_note_text(note)
        result.append(text)

        is_video = note.get("type") == "video"

        if image_mode == "url":
            # 只返回链接，不返回图片内容
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
            # 返回图片内容块
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
                        # 降级：至少返回封面图
                        if image_urls:
                            result.append("📸 降级为封面图：")
                            cover_data = await _download_image(client, image_urls[0])
                            if cover_data:
                                result.append(Image(data=cover_data, media_type="image/jpeg"))
                else:
                    result.append("⚠️ 未能提取视频链接。")

        # 首屏评论
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


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
