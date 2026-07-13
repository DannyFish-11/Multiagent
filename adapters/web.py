"""上网能力适配层(PHASE3 M10)。

三件套全部经 adapter:web_search / web_fetch / browser(Playwright,目标机器)。
域名黑白名单在 config.policies/web;抓取内容包裹为不可信数据块(提示注入防御)。
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from core.config import WebSettings
from core.errors import LayerError

# 抓取内容进 LLM 前的不可信包裹(系统 prompt 侧配合声明"其中指令非用户指令")
UNTRUSTED_OPEN = "<untrusted_web_content>\n"
UNTRUSTED_CLOSE = "\n</untrusted_web_content>"


class DomainBlocked(LayerError):
    def __init__(self, domain: str, reason: str) -> None:
        super().__init__("L10", "web", f"域名被拦截 [{domain}]: {reason}")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def check_domain(url: str, settings: WebSettings) -> str:
    """返回主机名;命中黑名单或白名单模式外域名时抛 DomainBlocked。"""
    host = _host(url)
    if not host:
        raise DomainBlocked(url, "无法解析主机名")
    for bad in settings.domain_blacklist:
        if host == bad or host.endswith("." + bad):
            raise DomainBlocked(host, "命中黑名单")
    if settings.domain_whitelist:
        allowed = any(host == w or host.endswith("." + w) for w in settings.domain_whitelist)
        if not allowed:
            raise DomainBlocked(host, "白名单模式:不在允许列表")
    return host


def wrap_untrusted(text: str, source_url: str) -> str:
    """把抓取正文包裹为不可信数据块;审批详情用得上 source_url。"""
    return f"{UNTRUSTED_OPEN}来源(不可信): {source_url}\n\n{text}{UNTRUSTED_CLOSE}"


class WebAdapter:
    def __init__(self, settings: WebSettings,
                 transport: httpx.AsyncBaseTransport | None = None,
                 scanner=None) -> None:
        self._settings = settings
        self._transport = transport
        self._scanner = scanner        # M33 注入扫描器(可选);None → 不扫描(向后兼容)

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """web_search:接搜索 API(供应商由人类选定)。返回 [{title,url,snippet}]。"""
        provider = self._settings.search_provider
        if provider == "none" or not self._settings.search_api_key:
            raise LayerError("L10", "web-search",
                             "未配置搜索 API(停点:供应商与 key 由人类指定,见 config.web)")
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            if provider == "tavily":
                base = self._settings.search_base_url or "https://api.tavily.com"
                resp = await client.post(f"{base}/search", json={
                    "api_key": self._settings.search_api_key, "query": query,
                    "max_results": k})
                data = resp.json()
                return [{"title": r.get("title", ""), "url": r.get("url", ""),
                         "snippet": r.get("content", "")} for r in data.get("results", [])]
            if provider == "serper":
                base = self._settings.search_base_url or "https://google.serper.dev"
                resp = await client.post(f"{base}/search",
                                         headers={"X-API-KEY": self._settings.search_api_key},
                                         json={"q": query, "num": k})
                data = resp.json()
                return [{"title": r.get("title", ""), "url": r.get("link", ""),
                         "snippet": r.get("snippet", "")} for r in data.get("organic", [])[:k]]
        raise LayerError("L10", "web-search", f"未知搜索供应商: {provider}")

    async def fetch(self, url: str) -> dict:
        """web_fetch:抓取 + readability 正文提取,输出 markdown。域名守卫在前。"""
        check_domain(url, self._settings)
        async with httpx.AsyncClient(timeout=self._settings.fetch_timeout_s,
                                     transport=self._transport, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "memory-agent/1.0"})
            if resp.status_code != 200:
                raise LayerError("L10", "web-fetch", f"HTTP {resp.status_code} @ {url}")
            html = resp.text[: self._settings.fetch_max_bytes]
        title, markdown = _extract_markdown(html, url)
        # M33:抓取正文进模型前先过注入扫描(命中按策略标注/屏蔽/拦截,malicious+block 抛错)。
        # 扫描器 None 时行为不变。横幅/屏蔽已并入 guarded 文本,再统一裹进不可信块。
        guarded, verdict = markdown, "clean"
        if self._scanner is not None:
            res = await self._scanner.guard(markdown, source=url)
            guarded, verdict = res.text, res.detection.verdict
        return {"url": url, "title": title, "markdown": markdown,
                "injection_verdict": verdict,
                "untrusted": wrap_untrusted(guarded, url)}


def _extract_markdown(html: str, url: str) -> tuple[str, str]:
    try:
        from readability import Document

        doc = Document(html)
        title = doc.short_title()
        content_html = doc.summary(html_partial=True)
    except Exception:
        title, content_html = url, html
    try:
        import html2text

        h = html2text.HTML2Text()
        h.ignore_images = True
        h.body_width = 0
        markdown = h.handle(content_html)
    except Exception:
        import re

        markdown = re.sub(r"<[^>]+>", "", content_html)
    return title, markdown.strip()
