"""core/tasks/web.py — reading a web page or a PDF in full.

A search result is a snippet, and a report written from snippets is a report
about headlines. This fetches the page itself and reduces it to its text: the
title, the headings and the paragraphs, without scripts, menus or footers.

fetch_document() returns the text itself — the research runner keeps it, so a
quote a model attributes to a page can be checked against what the page said.
fetch_page() is the tool's answer: the same text, cut to size and wrapped as
untrusted content (core/outcome.py).

PUBLIC ADDRESSES ONLY
    The URL comes from a model that has been reading pages written by strangers.
    A page can tell it to "fetch http://192.168.1.1/admin" — the user's router —
    or a service on the user's own machine, and the result would land in the
    agent's context. So every address, and every redirect along the way, is
    resolved first and refused if it points at a loopback, private, link-local
    or otherwise non-public network. Redirects are followed by hand for exactly
    that reason: a public page that redirects inward is still inward.

    It is a boundary, not a sandbox: a hostname that resolves differently the
    second time it is looked up could still slip through. It closes the easy
    and the accidental case, which is the one that happens.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

from core import outcome

_MAX_BYTES = 6_000_000          # a page larger than this is not an article
_MAX_PDF_BYTES = 20_000_000
_MAX_REDIRECTS = 4
_TIMEOUT = 20
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class Refused(ValueError):
    """The address may not be fetched."""


class FetchError(RuntimeError):
    """The address was allowed, but nothing readable came back."""


def check_url(url: str) -> str:
    """The URL if it may be fetched; raises Refused with a reason otherwise."""
    url = str(url or "").strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise Refused("only http and https addresses can be read")
    host = parts.hostname
    if not host:
        raise Refused("the address has no host")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise Refused(f"'{host}' does not resolve") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise Refused(f"'{host}' points at a local or private network address")
    return url


def _text_from_html(raw: bytes, encoding: str | None) -> tuple[str, str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser", from_encoding=encoding)
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header",
                     "aside", "form", "iframe", "button"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "pre", "blockquote"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            lines.append(f"\n## {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    body = "\n".join(lines).strip()
    if len(body) < 200:                     # pages that are not built from <p>
        body = root.get_text("\n", strip=True)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return title, body


def _text_from_pdf(data: bytes) -> str:
    import pdfplumber

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(data)
        tmp = Path(fh.name)
    try:
        out = []
        with pdfplumber.open(str(tmp)) as pdf:
            for page in pdf.pages[:40]:
                out.append(page.extract_text() or "")
        return "\n\n".join(out).strip()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def fetch_document(url: str) -> dict:
    """{"url": final address, "title", "text"}. Raises Refused or FetchError."""
    import requests

    current = check_url(url)
    resp = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            resp = requests.get(current, headers={"User-Agent": _UA, "Accept-Language": "*"},
                                timeout=_TIMEOUT, allow_redirects=False, stream=True)
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                nxt = resp.headers.get("Location")
                resp.close()
                if not nxt:
                    break
                current = check_url(urljoin(current, nxt))
                continue
            break
        else:
            raise FetchError("too many redirects")
    except (Refused, FetchError):
        raise
    except Exception as e:
        raise FetchError(f"could not load the page ({type(e).__name__})") from None

    if resp.status_code >= 400:
        resp.close()
        raise FetchError(f"the site answered {resp.status_code}")

    ctype = (resp.headers.get("Content-Type") or "").lower()
    limit = _MAX_PDF_BYTES if "pdf" in ctype or current.lower().endswith(".pdf") else _MAX_BYTES
    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(65536):
            size += len(chunk)
            if size > limit:
                break
            chunks.append(chunk)
    except Exception as e:
        raise FetchError(f"the download broke off ({type(e).__name__})") from None
    finally:
        resp.close()
    data = b"".join(chunks)

    try:
        if "pdf" in ctype or data[:5] == b"%PDF-":
            title, body = "", _text_from_pdf(data)
        elif "html" in ctype or b"<html" in data[:2000].lower():
            title, body = _text_from_html(data, resp.encoding)
        elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
            title, body = "", data.decode(resp.encoding or "utf-8", errors="replace")
        else:
            raise FetchError(f"not a readable page ({ctype or 'unknown type'})")
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"could not read the content ({type(e).__name__})") from None

    body = body.strip()
    if not body:
        raise FetchError("the page has no readable text (it may need a browser)")
    return {"url": current, "title": title, "text": body}


def format_page(doc: dict, max_chars: int = 15_000) -> str:
    max_chars = max(2_000, min(40_000, int(max_chars or 15_000)))
    body = doc["text"]
    text = f"URL: {doc['url']}\nTITLE: {doc.get('title') or ''}\n\n{body[:max_chars]}"
    if len(body) > max_chars:
        text += f"\n\n[page truncated at {max_chars} of {len(body)} characters]"
    return outcome.untrusted("web", text)


def fetch_page(url: str, max_chars: int = 15_000) -> str:
    try:
        return format_page(fetch_document(url), max_chars)
    except (Refused, FetchError) as e:
        return outcome.failed("fetch_page", str(e), url=url)
