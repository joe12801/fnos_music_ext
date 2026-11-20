"""mp3api-service 离线契约测试（不依赖真实上游 / fnOS）。

用 httpx.MockTransport 模拟 Music MP3 API，验证适配器满足 fnmusic-ext
的 musicdl 音源契约，以及 API Key 不外泄、Range/206 正确透传。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

APP_PATH = Path(__file__).with_name("app.py")
FAKE_MP3 = b"ID3" + b"x" * 2045
VIDEO = {
    "id": "dQw4w9WgXcQ",
    "title": "Rick Astley - Never Gonna Give You Up",
    "channel": "Rick Astley",
    "duration": 213,
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
}


def _handler(*, fail_search: bool = False, fail_music: bool = False,
             seen: list | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/search":
            if fail_search:
                return httpx.Response(502, json={"detail": "Sign in to confirm you're not a bot"})
            return httpx.Response(200, json={
                "query": "test", "count": 2,
                "results": [
                    {"id": VIDEO["id"], "title": VIDEO["title"],
                     "channel": VIDEO["channel"], "duration": VIDEO["duration"],
                     "thumbnail": VIDEO["thumbnail"]},
                    # 超过上游时长限制，搜索阶段就应被剔除，避免用户点到必然 413 的条目
                    {"id": "LONGFORM0001", "title": "Complete Nocturnes",
                     "channel": "HALIDONMUSIC", "duration": 6867, "thumbnail": ""},
                ],
            })
        if path == "/api/resolve":
            return httpx.Response(200, json={"video": VIDEO, "cache": {"hit": True},
                                            "audio_url": "/api/music?video_id=dQw4w9WgXcQ"})
        if path == "/api/music":
            if fail_music:
                return httpx.Response(502, json={"detail": "blocked"})
            rng = request.headers.get("range")
            if rng:
                body = FAKE_MP3[:1024]
                return httpx.Response(206, content=body, headers={
                    "content-type": "audio/mpeg", "content-length": str(len(body)),
                    "content-range": f"bytes 0-1023/{len(FAKE_MP3)}",
                    "accept-ranges": "bytes",
                })
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3)),
                "accept-ranges": "bytes",
            })
        return httpx.Response(404, json={"detail": "not found"})
    return handle


@pytest.fixture()
def client(monkeypatch):
    """主用例夹具：启动一次，自动退出 lifespan。"""
    seen: list[httpx.Request] = []
    monkeypatch.setenv("MP3API_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MP3API_API_KEY", "unit-test-key")
    monkeypatch.setenv("MP3API_BITRATE", "192")
    monkeypatch.setenv("MP3API_MAX_DURATION", "900")
    monkeypatch.setenv("MP3API_TRUST_ENV", "0")
    spec = importlib.util.spec_from_file_location("mp3api_app_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mp3api_app_test"] = mod
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
    assert j["source"] == "mp3api" and j["api_key_configured"] is True
    assert j["upstream_status"] == 200


def test_sources_contract(client):
    c, _, _ = client
    j = c.get("/sources").json()
    assert j == {"enabled": ["mp3api"], "registered": ["mp3api"]}


def test_search_contract(client):
    c, seen, _ = client
    r = c.get("/search", params={"keyword": "Rick", "limit": 3})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["errors"] == {} and j["engine"] == "mp3api"
    assert len(j["items"]) == 1  # 6867s 的超长条目被过滤
    assert all("LONGFORM0001" not in x["id"] for x in j["items"])
    item = j["items"][0]
    assert item["id"] == "mp3api:dQw4w9WgXcQ"       # 冒号 -> 代理生成 online:mp3api:...
    assert item["source"] == "mp3api"
    assert item["title"] == VIDEO["title"]
    assert item["artist"] == "Rick Astley"
    assert item["duration_s"] == 213 and item["ext"] == "mp3"
    assert item["cover_url"] == VIDEO["thumbnail"]
    # 不得把带密钥的直链或凭据写进搜索结果
    assert "download_url" not in item and "url" not in item
    assert "unit-test-key" not in r.text
    # 上游请求确实带了 API Key
    assert any(x.url.path == "/api/search" and x.headers.get("x-api-key") == "unit-test-key"
               for x in seen)


def test_info_contract(client):
    c, _, _ = client
    c.get("/search", params={"keyword": "Rick"})
    j = c.get("/info", params={"id": "mp3api:dQw4w9WgXcQ"}).json()
    assert j["ok"] is True and j["id"] == "mp3api:dQw4w9WgXcQ"
    assert j["title"] == VIDEO["title"] and j["duration_s"] == 213
    assert j["ext"] == "mp3" and j["lyric"] == ""
    assert j["cover_url"] == VIDEO["thumbnail"]


def test_stream_range_206(client):
    c, seen, _ = client
    r = c.get("/stream", params={"id": "mp3api:dQw4w9WgXcQ", "proxy": "true"},
              headers={"Range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-range"] == f"bytes 0-1023/{len(FAKE_MP3)}"
    assert "content-encoding" not in r.headers   # 代理要求 identity
    assert r.content == FAKE_MP3[:1024]
    upstream = [x for x in seen if x.url.path == "/api/music"][0]
    assert upstream.headers["range"] == "bytes=0-1023"
    assert upstream.headers["x-api-key"] == "unit-test-key"


def test_stream_full_200(client):
    c, _, _ = client
    r = c.get("/stream", params={"id": "online:mp3api:dQw4w9WgXcQ", "proxy": "true"})
    assert r.status_code == 200
    assert r.content == FAKE_MP3
    assert r.headers["content-length"] == str(len(FAKE_MP3))


def test_stream_proxy_false_never_leaks_upstream(client):
    c, _, _ = client
    r = c.get("/stream", params={"id": "mp3api:dQw4w9WgXcQ", "proxy": "false"},
              follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "upstream.test" not in loc and "unit-test-key" not in loc
    assert "proxy=true" in loc


def test_invalid_id_rejected(client):
    c, _, _ = client
    assert c.get("/info", params={"id": "../../etc/passwd"}).status_code == 400
    assert c.get("/stream", params={"id": "bad id with spaces", "proxy": "true"}).status_code == 400


def test_max_duration_is_configurable(monkeypatch):
    """MP3API_MAX_DURATION=0 时不过滤；较小值会把超长条目剔除。"""
    monkeypatch.setenv("MP3API_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MP3API_MAX_DURATION", "100")
    monkeypatch.setenv("MP3API_TRUST_ENV", "0")
    spec = importlib.util.spec_from_file_location("mp3api_app_duration", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mp3api_app_duration"] = mod
    spec.loader.exec_module(mod)
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(_handler()), *a, **kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        assert c.get("/search", params={"keyword": "x"}).json()["items"] == []


def test_search_upstream_failure_is_soft(monkeypatch):
    """上游 502 时返回 ok=False + errors（代理据此判定硬失败），不抛 500。"""
    monkeypatch.setenv("MP3API_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MP3API_API_KEY", "k")
    monkeypatch.setenv("MP3API_TRUST_ENV", "0")
    spec = importlib.util.spec_from_file_location("mp3api_app_fail", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mp3api_app_fail"] = mod
    spec.loader.exec_module(mod)
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(_handler(fail_search=True)), *a, **kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        j = c.get("/search", params={"keyword": "x"}).json()
    assert j["ok"] is False and j["items"] == [] and "502" in str(j["errors"])


def test_stream_upstream_failure_returns_502(monkeypatch):
    monkeypatch.setenv("MP3API_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MP3API_TRUST_ENV", "0")
    spec = importlib.util.spec_from_file_location("mp3api_app_fail2", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mp3api_app_fail2"] = mod
    spec.loader.exec_module(mod)
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(_handler(fail_music=True)), *a, **kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        r = c.get("/stream", params={"id": "mp3api:dQw4w9WgXcQ", "proxy": "true"})
    assert r.status_code == 502
