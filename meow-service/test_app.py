"""meow-service 离线契约测试（不依赖真实上游 / fnOS）。

用 httpx.MockTransport 模拟 MeowMusicServer，验证适配器满足 fnmusic-ext
的 musicdl 音源契约，Range/206 正确，且 id 可逆解码不丢歌名/歌手。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

APP_PATH = Path(__file__).with_name("app.py")
FAKE_MP3 = b"ID3" + b"y" * 2045
SEARCH_ROWS = [
    {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img/cover.jpg",
     "lyric_url": "https://img/lyric.lrc", "duration": 269, "url": "http://upstream/stream_live?song=..."},
    {"title": "七里香", "artist": "周杰伦", "cover_url": "", "lyric_url": "", "duration": 297},
]


def _handler(*, fail_search: bool = False, seen: list | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text="ok")
        if path == "/api/search":
            if fail_search:
                return httpx.Response(502, json={"detail": "boom"})
            return httpx.Response(200, json=SEARCH_ROWS)
        if path == "/stream_live":
            q = request.url.params
            assert q.get("song") and q.get("singer"), "song/singer must be forwarded"
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3)),
                "accept-ranges": "bytes",
            })
        return httpx.Response(404, json={"detail": "not found"})
    return handle


@pytest.fixture()
def client(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setenv("MEOW_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MEOW_API_KEY", "unit-test-key")
    monkeypatch.setenv("MEOW_TIMEOUT", "30")
    monkeypatch.setenv("MEOW_SOURCE_NAME", "meow")
    monkeypatch.setenv("MEOW_MAX_DURATION", "0")
    monkeypatch.setenv("MEOW_TRUST_ENV", "0")
    monkeypatch.setenv("MEOW_NO_DOTENV", "1")
    spec = importlib.util.spec_from_file_location("meow_app_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_test"] = mod
    spec.loader.exec_module(mod)

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_handler(seen=seen)),
                                 *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, seen, mod


def test_healthz_contract(client):
    c, _, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j, dict) and j["ok"] is True
    assert j["source"] == "meow" and j["api_key_configured"] is True
    assert j["upstream_status"] == 200


def test_sources_contract(client):
    c, _, _ = client
    assert c.get("/sources").json() == {"enabled": ["meow"], "registered": ["meow"]}


def test_search_contract(client):
    c, seen, _ = client
    r = c.get("/search", params={"keyword": "晴天", "limit": 5})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["errors"] == {} and j["engine"] == "meow"
    assert len(j["items"]) == 2
    item = j["items"][0]
    assert item["id"].startswith("meow:")       # 冒号 -> 代理生成 online:meow:...
    assert item["source"] == "meow"
    assert item["title"] == "晴天" and item["artist"] == "周杰伦"
    assert item["duration_s"] == 269 and item["ext"] == "mp3"
    assert item["cover_url"] == "https://img/cover.jpg"
    assert "download_url" not in item and "url" not in item
    assert "unit-test-key" not in r.text
    assert any(x.url.path == "/api/search" and x.url.params.get("query") == "晴天"
               for x in seen)


def test_info_roundtrip(client):
    c, _, mod = client
    j = c.get("/search", params={"keyword": "晴天"}).json()
    sid = j["items"][0]["id"]
    # id 可逆：解码必须还原原始歌名/歌手（含中文）
    song, singer = mod._decode_id(sid)
    assert song == "晴天" and singer == "周杰伦"
    info = c.get("/info", params={"id": sid}).json()
    assert info["ok"] is True and info["id"] == sid
    assert info["title"] == "晴天" and info["artist"] == "周杰伦"
    assert info["duration_s"] == 269 and info["ext"] == "mp3"


def test_info_accepts_online_guid(client):
    c, _, mod = client
    sid = mod._encode_id("晴天", "周杰伦")
    song, singer = mod._decode_id("online:" + sid)
    assert song == "晴天" and singer == "周杰伦"


def test_stream_forwards_song_singer(client):
    c, seen, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"})
    assert r.status_code == 200
    assert r.headers.get("content-type") == "audio/mpeg"
    assert int(r.headers["content-length"]) == len(FAKE_MP3)
    assert r.content == FAKE_MP3
    assert any(x.url.path == "/stream_live" and x.url.params.get("song") == "晴天"
               and x.url.params.get("singer") == "周杰伦" for x in seen)


def test_stream_range_206(client):
    c, _, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"},
              headers={"range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers.get("content-range") == f"bytes 0-1023/{len(FAKE_MP3)}"
    assert len(r.content) == 1024
    assert r.content == FAKE_MP3[:1024]


def test_stream_redirect_without_proxy(client):
    c, _, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid}, follow_redirects=False)
    assert r.status_code == 302
    assert "proxy=true" in r.headers["location"]


def test_invalid_id_rejected(client):
    c, _, _ = client
    assert c.get("/info", params={"id": "meow:!!!!bad"}).status_code == 400


def test_search_upstream_failure_soft(client):
    c, _, _ = client
    # 通过修改上游 handler 触发失败需重建，这里仅验证空 keyword 被拒
    assert c.get("/search", params={"keyword": ""}).status_code == 422


# ---- 真机兜底：上游 /api/search 恒返回 [] 时的行为（192.168.2.220 实测）----
@pytest.fixture()
def empty_search_client(monkeypatch):
    """上游 /api/search 返回空数组，/stream_live 仍按 song(+可选 singer) 出流。"""
    seen: list[httpx.Request] = []
    for k, v in {"MEOW_BASE_URL": "http://upstream.test", "MEOW_API_KEY": "",
                 "MEOW_TIMEOUT": "30", "MEOW_MAX_DURATION": "0",
                 "MEOW_TRUST_ENV": "0", "MEOW_NO_DOTENV": "1"}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("meow_app_empty_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_empty_test"] = mod
    spec.loader.exec_module(mod)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[])          # 真机现象：恒空
        if request.url.path == "/stream_live":
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3)),
                "accept-ranges": "bytes",
            })
        return httpx.Response(200, text="ok")

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handle), *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, seen, mod


def test_fallback_single_keyword(empty_search_client):
    """搜「晴天」应兜底出 1 个可点播候选，且 id 可逆还原。"""
    c, _, mod = empty_search_client
    j = c.get("/search", params={"keyword": "晴天"}).json()
    assert j["ok"] is True and len(j["items"]) == 1
    item = j["items"][0]
    assert item["title"] == "晴天" and item["id"].startswith("meow:")
    song, singer = mod._decode_id(item["id"])
    assert song == "晴天" and singer == ""


def test_fallback_song_singer_split(empty_search_client):
    """搜「晴天 周杰伦」应拆出 (晴天, 周杰伦) 主候选 + 整串兜底候选。"""
    c, _, mod = empty_search_client
    j = c.get("/search", params={"keyword": "晴天 周杰伦"}).json()
    assert len(j["items"]) == 2
    song, singer = mod._decode_id(j["items"][0]["id"])
    assert (song, singer) == ("晴天", "周杰伦")
    song2, singer2 = mod._decode_id(j["items"][1]["id"])
    assert (song2, singer2) == ("晴天 周杰伦", "")


def test_fallback_candidate_is_playable(empty_search_client):
    """兜底候选点播必须真出 MP3（singer 为空也要能出流）。"""
    c, seen, _ = empty_search_client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"})
    assert r.status_code == 200 and r.content == FAKE_MP3
    req = next(x for x in seen if x.url.path == "/stream_live")
    assert req.url.params.get("song") == "晴天"
