"""meow-service · fnmusic-ext 的第五音源适配器（Meow 聚合总源）。

把自建的 MeowMusicServer（Go，10+ 音源聚合：酷我/网易/xfabe/QQ/酷狗/
NCM/GDStudio/mp3api/yuafeng…）包装成 fnmusic-ext 已有的 musicdl 音源 HTTP
契约。这样代理层无需为它重写取链 / Range / 边听边存 / 转码 / 歌词等链路：
所有非 netease / 非 lx 的 GUID 都会自然落到 musicdl 分支。

与 mp3api-service 完全同构，区别只在「上游」：
    mp3api-service -> mp3.994938.xyz  (/api/search /api/resolve /api/music)
    meow-service    -> MeowMusicServer(/api/search /stream_live)

对外端点（与 musicdl-service 对齐）:
    GET /healthz                      健康检查
    GET /sources                      平台列表
    GET /search?keyword=&limit=       搜索，返回 {"ok":true,"items":[...],"errors":{},...}
    GET /info?id=meow:<b64>           曲目元数据
    GET /stream?id=meow:<b64>&proxy=true   MP3 字节流，支持 Range / 206

上游 MeowMusicServer:
    GET /api/search?query=<词>        -> [{title, artist, cover_url, lyric_url, duration}]
    GET /stream_live?song=&singer=    -> 定长 MP3 字节流（Content-Length，无 chunked）

id 设计：Meow 搜索结果没有稳定 id，因此用可逆 base64url(song \\x1f singer)
编码，保证 /info、/stream 无状态反查出 song/singer，适配器重启也不失效。
GUID 由代理生成 online:meow:<b64>，既不是 netease 也不是 lx，自动落 musicdl 分支。

安全约定：
  * 不实现 /internal/resolve 与 /internal/stream —— 播放直连会把上游地址暴露给
    客户端。启用本音源时必须保持 FNMUSIC_MEOW_DIRECT_PLAY / FNMUSIC_MUSICDL_DIRECT_PLAY=false。
  * id 一律形如 "meow:<b64url>"；GUID 由代理生成 online:meow:<b64url>。

合规：仅用于你拥有版权或已获授权的内容、个人 / 家庭自用，请勿公开分发或商用。
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse


def _load_dotenv() -> None:
    """从项目 .env 补齐 MEOW_*（仅当进程环境未显式设置该键时）。

    Docker / native 两种形态都由 supervisor 注入 FNMUSIC_ENV_FILE，适配器只读
    该文件即可拿到上游地址；直接 uvicorn 本机启动时回退到仓库根 / 服务目录
    / cwd 下的 .env。设置 MEOW_NO_DOTENV=1 可完全关闭此行为（离线测试用）。
    """
    if os.environ.get("MEOW_NO_DOTENV", "").strip().lower() in ("1", "true", "yes"):
        return
    keys = (
        "MEOW_BASE_URL", "MEOW_API_KEY", "MEOW_TIMEOUT", "MEOW_SOURCE_NAME",
        "MEOW_MAX_DURATION", "MEOW_TRUST_ENV", "MEOW_LOG_LEVEL",
    )
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env_file = os.environ.get("FNMUSIC_ENV_FILE", "").strip()
    if env_file:
        candidates.append(Path(env_file))
    candidates += [here.parent / ".env", here / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            for line in resolved.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, _, raw = line.partition("=")
                key = key.strip()
                if key not in keys or key in os.environ:
                    continue
                value = raw.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ[key] = value
        except OSError:
            continue


_load_dotenv()

LOG = logging.getLogger("meow")
logging.basicConfig(level=os.environ.get("MEOW_LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------- 配置
def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


BASE_URL = _env_str("MEOW_BASE_URL", "http://192.168.2.220:2233").rstrip("/")
API_KEY = _env_str("MEOW_API_KEY", "")
TIMEOUT = float(os.environ.get("MEOW_TIMEOUT", "300") or 300)
SOURCE = _env_str("MEOW_SOURCE_NAME", "meow")
MAX_DURATION = _env_int("MEOW_MAX_DURATION", 0, 0, 86400)  # 0 = 不过滤
TRUST_ENV = _env_str("MEOW_TRUST_ENV", "0").lower() in ("1", "true", "yes")

# 搜索结果 id -> item，仅用于 /info 补全（元数据失败时退回缓存）
_ITEMS: dict[str, dict] = {}
# base64url 字符集，长度放宽到覆盖较长中文歌名编码
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,600}$")
_SEP = "\x1f"  # unit separator，避免与歌名/歌手里的常见字符冲突


def _upstream_headers(extra: dict | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json, audio/mpeg;q=0.9, */*;q=0.8"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


def _encode_id(song: str, singer: str) -> str:
    raw = f"{song}{_SEP}{singer}".encode("utf-8")
    return SOURCE + ":" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_id(raw: str) -> tuple[str, str]:
    """接受 'meow:<b64>' / 'online:meow:<b64>'，返回 (song, singer)。"""
    s = str(raw or "").strip()
    if s.startswith("online:"):
        s = s[len("online:"):]
    if ":" in s:
        s = s.split(":", 1)[1]
    if not _ID_RE.match(s):
        raise HTTPException(400, "invalid song id")
    padded = s + "=" * (-len(s) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise HTTPException(400, "invalid song id encoding")
    song, sep, singer = decoded.partition(_SEP)
    if not song or not sep:
        raise HTTPException(400, "invalid song id payload")
    return song, singer


def _error(keyword: str, message: str) -> dict:
    return {"ok": False, "cached": False, "keyword": keyword, "items": [],
            "errors": {SOURCE: message}, "engine": SOURCE}


def _fallback_raw_items(keyword: str, limit: int) -> list[dict]:
    """上游 /api/search 为空时的兜底候选。

    MeowMusicServer 的真实能力是「歌名(+歌手) → 最佳单曲」的点歌核心
    (/stream_live?song=&singer=，10+ 音源聚合、定长 audio/mpeg)，而它的
    /api/search 是个弱接口：只读工作目录下的 sources.json + 调用 yuafeng
    旧 API，实测（192.168.2.220）恒返回空数组。因此当上游给不出曲目列表时，
    把用户输入的关键词解析成 1~2 个可点播候选，点击后由 /stream 真实出音频：
      * "晴天"          -> (song="晴天", singer="")
      * "晴天 周杰伦"   -> (song="晴天", singer="周杰伦") + (song="晴天 周杰伦", singer="")
    这样既保留「总源即点即播」的语义，又完全不需要改动上游 Go 服务。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    parts = [p for p in re.split(r"\s+", kw) if p]
    combos: list[tuple[str, str]] = []
    if len(parts) >= 2:
        combos.append((parts[0], " ".join(parts[1:])))  # 歌名在前：晴天 周杰伦
    combos.append((kw, ""))                            # 整串当歌名，最稳（已实测可播）
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for song, singer in combos:
        if (song, singer) in seen:
            continue
        seen.add((song, singer))
        out.append({"title": song, "artist": singer, "duration": 0, "cover_url": ""})
        if len(out) >= limit:
            break
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.upstream = httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(TIMEOUT, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
        trust_env=TRUST_ENV,
    )
    LOG.info("meow-service ready: upstream=%s key=%s source=%s timeout=%.0f",
             BASE_URL, "set" if API_KEY else "none", SOURCE, TIMEOUT)
    try:
        yield
    finally:
        await app.state.upstream.aclose()


app = FastAPI(title="meow-service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 契约端点
@app.get("/healthz")
async def healthz() -> dict:
    result: dict = {
        "ok": True,
        "source": SOURCE,
        "provider": "meow",
        "upstream": BASE_URL,
        "api_key_configured": bool(API_KEY),
        "cache_entries": len(_ITEMS),
        "checked_at": int(time.time()),
    }
    try:
        # Meow 根路径有 indexHandler，用它做轻量探活
        resp = await app.state.upstream.get("/", headers=_upstream_headers(), timeout=3.0)
        result["upstream_status"] = resp.status_code
        if resp.status_code >= 500:
            result["ok"] = False
            result["error"] = "upstream returned 5xx"
    except Exception as exc:  # noqa: BLE001 - 健康检查只报告，不抛
        result["ok"] = False
        result["error"] = type(exc).__name__
    return result


@app.get("/sources")
async def sources() -> dict:
    return {"enabled": [SOURCE], "registered": [SOURCE]}


@app.get("/search")
async def search(
    keyword: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=30),
    sources: str = Query("", description="musicdl 平台白名单，本音源忽略"),
) -> dict:
    try:
        resp = await app.state.upstream.get(
            "/api/search",
            params={"query": keyword},
            headers=_upstream_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("search upstream error: %s", type(exc).__name__)
        return _error(keyword, f"upstream error: {type(exc).__name__}")
    if resp.status_code >= 400:
        return _error(keyword, f"upstream returned {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return _error(keyword, "upstream returned invalid JSON")

    # Meow /api/search 返回数组：[{title, artist, url, cover_url, lyric_url, duration}]
    raw_items = payload if isinstance(payload, list) else (payload.get("results") or payload.get("items") or [])
    if not raw_items:
        # 上游 /api/search 是弱接口：无 sources.json 时恒返回空数组（真机实测）。
        # 兜底成可点播候选，点击后 /stream 走 /stream_live 真实出音频。
        raw_items = _fallback_raw_items(keyword, limit)
    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        artist = str(raw.get("artist") or "").strip()
        try:
            duration = int(raw.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if MAX_DURATION and duration > MAX_DURATION:
            LOG.info("skip over-long track %s (%ss > %ss)", title, duration, MAX_DURATION)
            continue
        song_id = _encode_id(title, artist)
        cover = str(raw.get("cover_url") or "")
        item = {
            "id": song_id,
            "source": SOURCE,
            "title": title,
            "artist": artist,
            "album": "",
            "duration_s": duration,
            "ext": "mp3",
            "file_size": 0,
            "protocol": "http",
            "delivery": "direct",
            "bitrate": 320000,
            "samplerate": 0,
            "codec": "mp3",
            "channels": 2,
            "quality": "unknown",
            "cover_url": cover,
            # 刻意不返回 download_url / url：由本服务内部取流
        }
        _ITEMS[song_id] = item
        items.append(item)
        if len(items) >= limit:
            break
    if len(_ITEMS) > 2000:
        for stale in list(_ITEMS)[: len(_ITEMS) - 2000]:
            _ITEMS.pop(stale, None)
    LOG.info("search '%s' -> %d items", keyword, len(items))
    return {"ok": True, "cached": False, "keyword": keyword,
            "items": items, "errors": {}, "engine": SOURCE}


@app.get("/info")
async def info(id: str = Query(...)) -> dict:
    song, singer = _decode_id(id)
    song_id = _encode_id(song, singer)
    cached = _ITEMS.get(song_id, {})
    return {
        "ok": True,
        "id": song_id,
        "source": SOURCE,
        "title": str(cached.get("title") or song),
        "artist": str(cached.get("artist") or singer),
        "album": str(cached.get("album") or ""),
        "duration_s": int(cached.get("duration_s") or 0),
        "ext": "mp3",
        "protocol": "http",
        "delivery": "direct",
        "quality": "unknown",
        "bitrate": 320000,
        "samplerate": 0,
        "file_size": 0,
        "cover_url": str(cached.get("cover_url") or ""),
        "lyric": "",
    }


@app.get("/stream")
async def stream(
    request: Request,
    id: str = Query(...),
    proxy: bool = Query(False, description="false=302 到本服务 proxy=true（不暴露上游地址）"),
    quality: str = Query("", description="保留参数以兼容 musicdl 契约，本音源固定 mp3"),
):
    song, singer = _decode_id(id)
    song_id = _encode_id(song, singer)
    if not proxy:
        return RedirectResponse("/stream?id=" + song_id + "&proxy=true", status_code=302)

    # Meow /stream_live 定长返回；先把完整流读到内存，再按 Range 切片返回，
    # 以兼容 fnos proxy 的顺序播放（200）与 Range/206（边听边存 / seek）两种场景。
    headers = _upstream_headers()
    headers["Accept-Encoding"] = "identity"
    client: httpx.AsyncClient = app.state.upstream
    req = client.build_request(
        "GET", "/stream_live",
        params={"song": song, "singer": singer},
        headers=headers,
    )
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("stream upstream error for %s - %s: %s", song, singer, type(exc).__name__)
        raise HTTPException(502, f"upstream request failed: {type(exc).__name__}")

    if resp.status_code >= 400:
        body = (await resp.aread())[:300].decode("utf-8", "replace")
        await resp.aclose()
        raise HTTPException(502, f"upstream returned {resp.status_code}: {body}")

    content_type = resp.headers.get("content-type") or "audio/mpeg"
    chunks: list[bytes] = []
    try:
        async for chunk in resp.aiter_bytes(64 * 1024):
            if chunk:
                chunks.append(chunk)
    finally:
        await resp.aclose()
    payload = b"".join(chunks)
    total = len(payload)

    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Type": content_type,
    }

    # 解析 Range: bytes=START-END
    range_header = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if m and total > 0:
        start_s, end_s = m.group(1), m.group(2)
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        else:
            # suffix range: bytes=-N
            n = int(end_s) if end_s else 0
            start = max(0, total - n) if n else 0
            end = total - 1
        if start >= total:
            raise HTTPException(416, "range not satisfiable")
        end = min(end, total - 1)
        body = payload[start:end + 1]
        out_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        out_headers["Content-Length"] = str(len(body))
        return StreamingResponse(iter([body]), status_code=206, headers=out_headers)

    out_headers["Content-Length"] = str(total)
    return StreamingResponse(iter([payload]) if payload else iter([]), status_code=200, headers=out_headers)


# 直连 (/internal/resolve, /internal/stream) 刻意不实现：那会把上游地址交给客户端。
# 请在 .env 中保持 FNMUSIC_MEOW_DIRECT_PLAY / FNMUSIC_MUSICDL_DIRECT_PLAY=false。
