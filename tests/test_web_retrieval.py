"""Behavior checks for discovery, source snapshots and the public-network boundary."""

import asyncio
import json
import socket

import httpx
import pytest

from agentgpt import web_retrieval as web
from agentgpt.config import Settings
from agentgpt.mcp_manager import ToolManager
from agentgpt.store import Store
from agentgpt.tools import WebFetchTool


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    observed = {}

    def client(**kwargs):
        observed.update(kwargs)
        kwargs.pop("proxy", None)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", client)
    return observed


@pytest.mark.asyncio
async def test_discovery_domains_dedup_and_snippet_labels(monkeypatch):
    captured = {}

    class Search:
        def __init__(self, **kwargs):
            pass

        def news(self, query, **kwargs):
            captured.update(query=query, **kwargs)
            return [
                {"url": "https://docs.example.org/a#one", "body": "A" * 1700},
                {"url": "https://docs.example.org/a#two", "body": "duplicate"},
                {"url": "https://example.org.evil.test/a", "body": "wrong host"},
                {"url": "https://blocked.example.org/a", "body": "blocked"},
            ]

    monkeypatch.setattr(web, "DDGS", Search)
    result = await web.search_web(
        web.SearchInput(
            query="test",
            topic="news",
            time_range="w",
            include_domains=["example.org"],
            exclude_domains=["blocked.example.org"],
        ),
        Settings(search_provider="ddgs"),
    )
    assert result["code"] == "HIT" and len(result["sources"]) == 1
    assert captured["timelimit"] == "w"
    source = result["sources"][0]
    assert source["url"] == "https://docs.example.org/a"
    assert source["content_kind"] == "search_snippet" and source["truncated"]


@pytest.mark.asyncio
async def test_tavily_contract_and_explicit_auto_fallback(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(503)

    mock_http(monkeypatch, handler)

    class Search:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return []

    monkeypatch.setattr(web, "DDGS", Search)
    result = await web.search_web(
        web.SearchInput(query="topic"),
        Settings(
            search_provider="auto",
            tavily_api_key="test-key",
        ),
    )
    assert result["code"] == "MISS" and result["provider"] == "ddgs"
    assert result["warnings"] and not calls[0]["include_answer"]
    with pytest.raises(RuntimeError, match="Tavily search failed"):
        await web.search_web(
            web.SearchInput(query="topic"),
            Settings(
                search_provider="tavily",
                tavily_api_key="test-key",
            ),
        )


@pytest.mark.asyncio
async def test_tavily_success(monkeypatch):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/",
                        "title": "Example",
                        "content": "text",
                        "published_date": "2026-10-30",
                    }
                ],
            },
        ),
    )
    result = await web.search_web(
        web.SearchInput(query="topic"),
        Settings(
            search_provider="tavily",
            tavily_api_key="test-key",
        ),
    )
    assert result["sources"][0]["published_at"] == "2026-10-30"
    assert result["provider"] == "tavily" and not result["warnings"]


@pytest.mark.asyncio
async def test_ddgs_no_results_is_distinct_from_provider_failure(monkeypatch):
    class Search:
        error = "No results found."

        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise web.DDGSException(self.error)

    monkeypatch.setattr(web, "DDGS", Search)
    result = await web.search_web(web.SearchInput(query="topic"), Settings(search_provider="ddgs"))
    assert result["code"] == "MISS"
    Search.error = "connection failed"
    with pytest.raises(RuntimeError, match="DDGS search failed"):
        await web.search_web(web.SearchInput(query="topic"), Settings(search_provider="ddgs"))


def test_article_extraction_removes_scripts_navigation_and_preserves_locations():
    result = web.parse_source(
        "https://example.org/",
        "text/html; charset=utf-8",
        b"""
        <html><head><title>Example</title></head><body><nav>Menu</nav>
        <main><h1>Heading</h1><p>Real <b>evidence</b>.</p><script>attack()</script></main>
        <footer>Footer</footer></body></html>""",
    )
    assert result["title"] == "Example"
    assert [p["text"] for p in result["paragraphs"]] == ["Heading", "Real evidence."]
    assert result["paragraphs"][1]["paragraph"] == 2
    assert "attack" not in json.dumps(result)


def test_role_main_and_pdf_pages():
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    html = web.parse_source(
        "https://example.org/",
        "text/html",
        b"""
        <div>Navigation</div><div role="main"><p>Evidence</p></div><div>Sidebar</div>""",
    )
    assert [p["text"] for p in html["paragraphs"]] == ["Evidence"]
    writer = PdfWriter()
    for value in (b"First page", b"Second page"):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 250 Td (" + value + b") Tj ET")
        page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    result = web.parse_source("https://example.org/test.pdf", "application/pdf", output.getvalue())
    assert [(p["page"], p["text"]) for p in result["paragraphs"]] == [
        (1, "First page"),
        (2, "Second page"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://u:p@example.org/",
        "http://example.org:8000/",
        "http://example.org\\@local/",
    ],
)
def test_unsafe_url_syntax(url):
    with pytest.raises(ValueError):
        web.normalize_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "192.168.30.215",
        "169.254.169.254",
        "::1",
        "224.0.0.1",
        "ff02::1",
    ],
)
async def test_private_dns_results_are_rejected(monkeypatch, address):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="public network"):
        await web.public_address("https://example.org/")


@pytest.mark.asyncio
async def test_fetch_pins_address_and_rechecks_redirect(monkeypatch):
    checked, sent = [], []

    async def resolve(url):
        checked.append(url)
        if "internal" in url:
            raise ValueError("private redirect")
        return "93.184.216.34"

    def handler(request):
        sent.append(request)
        return httpx.Response(302, headers={"location": "http://internal/secret"})

    monkeypatch.setattr(web, "public_address", resolve)
    mock_http(monkeypatch, handler)
    with pytest.raises(ValueError, match="private redirect"):
        await web.download("https://example.org/", Settings())
    assert len(sent) == 1 and len(checked) == 2
    assert sent[0].url.host == "93.184.216.34"
    assert sent[0].headers["host"] == "example.org"
    assert sent[0].extensions["sni_hostname"] == "example.org"


@pytest.mark.asyncio
async def test_download_size_and_success(monkeypatch):
    async def resolve(url):
        return "93.184.216.34"

    monkeypatch.setattr(web, "public_address", resolve)
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"a" * 1001,
        ),
    )
    with pytest.raises(ValueError, match="size limit"):
        await web.download("https://example.org/", Settings(web_max_bytes=1000))
    url, kind, body = await web.download("https://example.org/", Settings())
    assert url == "https://example.org/" and kind == "text/plain" and len(body) == 1001


@pytest.mark.asyncio
async def test_explicit_proxy_preserves_pinned_target(monkeypatch):
    async def resolve(url):
        return "93.184.216.34"

    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"source")

    monkeypatch.setattr(web, "public_address", resolve)
    options = mock_http(monkeypatch, handler)
    await web.download("https://example.org/", Settings(search_proxy="http://proxy.test:18090"))
    assert options["proxy"] == "http://proxy.test:18090" and options["trust_env"] is False
    import ssl

    assert options["verify"].check_hostname is True
    assert options["verify"].verify_mode == ssl.CERT_REQUIRED
    assert options["verify"].source_hostname == "example.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["93.184.216.34", "127.0.0.1"])
async def test_proxy_dns_fallback_still_rejects_private_addresses(monkeypatch, address):
    async def resolve(url):
        raise socket.gaierror("DNS unavailable")

    def handler(request):
        if request.url.host == "cloudflare-dns.com":
            return httpx.Response(
                200,
                json={
                    "Status": 0,
                    "Answer": [{"type": 1, "data": address}],
                },
            )
        assert request.url.host == address
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"source")

    monkeypatch.setattr(web, "public_address", resolve)
    mock_http(monkeypatch, handler)
    settings = Settings(search_proxy="http://proxy.test:18090")
    if address == "127.0.0.1":
        with pytest.raises(ValueError, match="public network"):
            await web.download("https://example.org/", settings)
    else:
        _, _, body = await web.download("https://example.org/", settings)
        assert body == b"source"


@pytest.mark.asyncio
async def test_snapshots_survive_reopen_are_isolated_and_can_be_paged(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.db")
    await store.initialize()
    snapshot = web.parse_source("https://example.org/", "text/plain", b"a" * 2000)
    record = await store.save_web_evidence("alice", snapshot)
    same = await store.save_web_evidence("alice", snapshot)
    assert same["evidence_id"] == record["evidence_id"]
    store = Store(store.path)
    with pytest.raises(ValueError, match="not found"):
        await store.web_evidence("bob", record["evidence_id"])
    tool = WebFetchTool(Settings(), store)
    first = await tool.invoke(
        {"user_id": "alice", "evidence_id": record["evidence_id"], "max_chars": 500}
    )
    second = await tool.invoke(
        {
            "user_id": "alice",
            "evidence_id": record["evidence_id"],
            "offset": first["next_offset"],
            "max_chars": 500,
        }
    )
    assert len(first["text"]) == 500 and second["paragraphs"][0]["char_start"] == 500
    assert "user_id" not in first
    updated = await store.save_web_evidence(
        "alice",
        {
            **snapshot,
            "paragraphs": [
                {"paragraph": 1, "page": None, "text": "updated"},
            ],
        },
    )
    assert updated["evidence_id"] != record["evidence_id"]
    assert (await store.web_evidence("alice", record["evidence_id"]))["content_hash"] == record[
        "content_hash"
    ]


@pytest.mark.asyncio
async def test_real_mcp_snapshot_read_injects_owner(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(settings.database)
    await store.initialize()
    saved = await store.save_web_evidence(
        "alice",
        web.parse_source(
            "https://example.org/",
            "text/plain",
            b"Evidence belonging to Alice",
        ),
    )
    manager = ToolManager(settings, store)
    await manager.start()
    try:
        alice = manager.tools_for("alice", ["builtin__web_fetch"])[0]
        assert "user_id" not in alice.args
        result = json.loads(await alice.ainvoke({"evidence_id": saved["evidence_id"]}))
        assert "Alice" in result["text"]
        bob = manager.tools_for("bob", ["builtin__web_fetch"])[0]
        with pytest.raises(ValueError, match="not found"):
            await bob.ainvoke({"evidence_id": saved["evidence_id"]})
        assert manager.replay_safe("builtin__web_fetch")
    finally:
        await manager.close()
