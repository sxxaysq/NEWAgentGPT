"""Public web discovery and bounded, address-pinned source retrieval."""

import asyncio
import hashlib
import io
import ipaddress
import re
import socket
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import httpx
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pypdf import PdfReader


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)
    topic: Literal["general", "news"] = "general"
    time_range: Literal["d", "w", "m", "y"] | None = None
    include_domains: list[str] = Field(default_factory=list, max_length=5)
    exclude_domains: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def domains(cls, values):
        output = []
        for value in values:
            domain = value.lower().strip().encode("idna").decode("ascii")
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
                raise ValueError("Domains must be hostnames without paths or schemes")
            output.append(domain)
        return list(dict.fromkeys(output))


class FetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, description="Injected by the authenticated application")
    url: str = Field(default="", max_length=2000)
    evidence_id: str = Field(default="", max_length=80)
    offset: int = Field(default=0, ge=0, le=200_000)
    max_chars: int = Field(default=8000, ge=500, le=16000)

    @model_validator(mode="after")
    def source(self):
        if bool(self.url) == bool(self.evidence_id):
            raise ValueError("Provide exactly one of url or evidence_id")
        return self


def normalize_url(value):
    """Preserve query/path semantics; drop fragments and reject ambiguous authorities."""
    if re.search(r"[\x00-\x20\\]", value):
        raise ValueError("Invalid URL")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError("Only public HTTP(S) URLs without credentials are supported")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Only HTTP(S) ports 80 and 443 are supported")
    host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    authority = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        authority += f":{parts.port}"
    return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))


def allowed_domain(url, includes, excludes):
    host = urlsplit(url).hostname or ""

    def matches(domain):
        return host == domain or host.endswith("." + domain)

    return (not includes or any(map(matches, includes))) and not any(map(matches, excludes))


async def public_address(url):
    host = urlsplit(url).hostname
    records = await asyncio.get_running_loop().getaddrinfo(
        host,
        urlsplit(url).port or (443 if url.startswith("https:") else 80),
        type=socket.SOCK_STREAM,
    )
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    return choose_public_address(addresses)


def choose_public_address(addresses):
    parsed = [ipaddress.ip_address(ip) for ip in addresses]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in parsed):
        raise ValueError("Web retrieval only permits public network addresses")
    return addresses[0]


async def proxy_address(url, settings):
    """Resolve via HTTPS only when local DNS fails and an explicit proxy is configured."""
    async with httpx.AsyncClient(
        proxy=settings.search_proxy, trust_env=False, timeout=settings.web_timeout
    ) as client:
        response = await client.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": urlsplit(url).hostname, "type": "A"},
            headers={"Accept": "application/dns-json"},
        )
        response.raise_for_status()
        answer = response.json()
        if answer.get("Status") != 0:
            raise ValueError("Public DNS resolution failed")
        return choose_public_address(
            [record["data"] for record in answer.get("Answer", []) if record.get("type") == 1]
        )


class SourceTLSContext(ssl.SSLContext):
    """Keep original-host verification when httpcore CONNECT uses a pinned IP."""

    def __new__(cls, hostname):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, hostname):
        self.source_hostname = hostname
        self.load_verify_locations(cafile=certifi.where())

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        return super().wrap_bio(
            incoming,
            outgoing,
            server_side=server_side,
            server_hostname=self.source_hostname,
            session=session,
        )


async def download(url, settings):
    """Resolve each redirect and connect to that exact address, preserving TLS SNI."""
    current = normalize_url(url)
    async with asyncio.timeout(settings.web_timeout):
        for _ in range(6):
            try:
                address = await public_address(current)
            except socket.gaierror:
                if not settings.search_proxy:
                    raise
                address = await proxy_address(current, settings)
            logical = httpx.URL(current)
            pinned = logical.copy_with(host=address)
            # A fresh client binds TLS identity to this redirect and isolates cookies.
            async with httpx.AsyncClient(
                trust_env=False,
                proxy=settings.search_proxy or None,
                timeout=settings.web_timeout,
                verify=SourceTLSContext(logical.host) if settings.search_proxy else True,
            ) as client:
                async with client.stream(
                    "GET",
                    pinned,
                    headers={
                        "Host": urlsplit(current).netloc,
                        "User-Agent": "AgentGPT-Research/0.1",
                        "Accept": "text/html,application/pdf,text/plain",
                    },
                    extensions={"sni_hostname": logical.host},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Redirect has no location")
                        current = normalize_url(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    size = response.headers.get("content-length", "")
                    if size.isdigit() and int(size) > settings.web_max_bytes:
                        raise ValueError("Source exceeds the download size limit")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > settings.web_max_bytes:
                            raise ValueError("Source exceeds the download size limit")
                    return current, content_type, bytes(body)
    raise ValueError("Too many redirects")


class PageText(HTMLParser):
    """Prefer article/main content; strip navigation and executable markup."""

    blocks = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "pre",
        "blockquote",
        "br",
    }
    ignored = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form"}
    void = {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "area", "embed"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.main_depths = []
        self.body, self.main, self.title = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.void:
            self.stack.append(tag)
            if tag in {"main", "article"} or dict(attrs).get("role") == "main":
                self.main_depths.append(len(self.stack))
        if tag in self.blocks:
            self.handle_data("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.blocks:
            self.handle_data("\n")
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag) :]
            self.main_depths = [depth for depth in self.main_depths if depth <= len(self.stack)]

    def handle_data(self, data):
        if "title" in self.stack:
            self.title.append(data)
        if "head" in self.stack or any(tag in self.ignored for tag in self.stack):
            return
        self.body.append(data)
        if self.main_depths:
            self.main.append(data)


def parse_source(url, content_type, raw):
    pages = []
    title = urlsplit(url).hostname
    truncated = False
    if "application/pdf" in content_type or raw.startswith(b"%PDF-"):
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF is not supported")
        title = str((reader.metadata or {}).get("/Title") or title)
        for number, page in enumerate(reader.pages[:50], 1):
            pages.append((number, page.extract_text() or ""))
        truncated = len(reader.pages) > 50
        kind = "pdf"
    else:
        match = re.search(r"charset=[\"']?([\w-]+)", content_type)
        encoding = match[1] if match else "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        if "html" in content_type or (not content_type and "<html" in text[:1000].lower()):
            parser = PageText()
            parser.feed(text)
            text = "".join(parser.main if "".join(parser.main).strip() else parser.body)
            title = "".join(parser.title).strip() or title
            kind = "html"
        elif content_type.startswith("text/"):
            kind = "text"
        else:
            raise ValueError("Only HTML, text and text-based PDF sources are supported")
        pages = [(None, text)]
    paragraphs, size = [], 0
    for page, text in pages:
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue
            remaining = 200_000 - size
            if remaining <= 0:
                truncated = True
                break
            if len(line) > remaining:
                line, truncated = line[:remaining], True
            paragraphs.append({"paragraph": len(paragraphs) + 1, "page": page, "text": line})
            size += len(line) + 1
    if not paragraphs:
        raise ValueError("No readable text; this source may need OCR or browser rendering")
    return {
        "url": url,
        "title": title[:500],
        "kind": kind,
        "paragraphs": paragraphs,
        "content_truncated": truncated,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def search_web(params, settings):
    provider = settings.search_provider
    if provider == "auto":
        provider = "tavily" if settings.tavily_api_key else "ddgs"
    warnings = []
    if provider == "tavily":
        if not settings.tavily_api_key:
            raise ValueError("TAVILY_API_KEY is required for SEARCH_PROVIDER=tavily")
        try:
            async with httpx.AsyncClient(
                timeout=settings.web_timeout, trust_env=False, proxy=settings.search_proxy or None
            ) as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + settings.tavily_api_key},
                    json={
                        "query": params.query,
                        "max_results": params.limit,
                        "topic": params.topic,
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                        "include_domains": params.include_domains,
                        "exclude_domains": params.exclude_domains,
                        **({"time_range": params.time_range} if params.time_range else {}),
                    },
                )
                response.raise_for_status()
                rows = response.json()["results"]
        except (httpx.HTTPError, KeyError, ValueError):
            if settings.search_provider != "auto":
                raise RuntimeError("Tavily search failed; check provider configuration") from None
            provider = "ddgs"
            warnings.append("Tavily unavailable; fell back to DDGS")
    if provider == "ddgs":
        query = params.query
        if len(params.include_domains) == 1:
            query += " site:" + params.include_domains[0]
        elif params.include_domains:
            query += " (" + " OR ".join("site:" + d for d in params.include_domains) + ")"
        query += "".join(" -site:" + d for d in params.exclude_domains)

        def run():
            client = DDGS(proxy=settings.search_proxy or None, timeout=settings.web_timeout)
            method = client.news if params.topic == "news" else client.text
            return list(
                method(query, max_results=min(params.limit * 2, 20), timelimit=params.time_range)
            )

        try:
            rows = await asyncio.to_thread(run)
        except DDGSException as exc:
            if str(exc).strip() != "No results found.":
                raise RuntimeError("DDGS search failed; check network or SEARCH_PROXY") from None
            rows = []
        except Exception:
            raise RuntimeError("DDGS search failed; check network or SEARCH_PROXY") from None
    sources, seen = [], set()
    for row in rows:
        try:
            url = normalize_url(row.get("url") or row.get("href") or "")
        except (ValueError, UnicodeError):
            continue
        if url in seen or not allowed_domain(url, params.include_domains, params.exclude_domains):
            continue
        seen.add(url)
        snippet = str(row.get("content") or row.get("body") or "")
        sources.append(
            {
                "source_id": "url_" + hashlib.sha256(url.encode()).hexdigest()[:20],
                "title": str(row.get("title") or "")[:500],
                "url": url,
                "text": snippet[:1600],
                "content_kind": "search_snippet",
                "truncated": len(snippet) > 1600,
                "published_at": row.get("published_date") or row.get("date"),
                "provider": provider,
            }
        )
        if len(sources) == params.limit:
            break
    return {
        "code": "HIT" if sources else "MISS",
        "query": params.query,
        "provider": provider,
        "warnings": warnings,
        "sources": sources,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "next_step": "Use web_fetch on relevant URLs to verify claims against source text.",
    }


def evidence_window(record, offset, max_chars):
    cursor, output = 0, []
    end = offset + max_chars
    for item in record["paragraphs"]:
        text = item["text"]
        start = max(0, offset - cursor)
        stop = min(len(text), end - cursor)
        if stop > start:
            output.append({**item, "text": text[start:stop], "char_start": cursor + start})
        cursor += len(text) + 1
    cursor = max(0, cursor - 1)
    return {key: value for key, value in record.items() if key not in {"paragraphs", "user_id"}} | {
        "code": "HIT" if output else "MISS",
        "paragraphs": output,
        "offset": offset,
        "total_chars": cursor,
        "next_offset": end if end < cursor else None,
        "text": "\n".join(item["text"] for item in output),
    }
