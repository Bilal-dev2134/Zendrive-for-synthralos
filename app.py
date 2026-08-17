"""Zendriver scrape service — the VPS side of the `zendriver` scraping engine.

Speaks the same contract as the other SynthralOS scraper VPS services:

    POST /scrape  {"url", "proxy"?, "wait_for"?, "wait_ms"?, "timeout"?}
      -> {"success": true,  "data": {"title", "url", "html"}}
      -> {"success": false, "error": "..."}          (HTTP 200 — clients read `success`)
    GET  /health  -> {"status": "ok"}

Deployed on Coolify, port 8895. Consumed by
`app.scraping.services.vps_client.fetch_zendriver`.

TWO THINGS HERE ARE LOAD-BEARING AND EASY TO GET WRONG
------------------------------------------------------
1. Proxy credentials. Chrome ignores the user:pass in `--proxy-server`, so an
   authenticated residential proxy (Webshare) needs the credentials supplied over
   CDP in response to `Fetch.authRequired`. Without this the browser silently
   egresses direct or 407s, which is indistinguishable from "the proxy didn't
   help" at the caller.

2. The proxy is a BROWSER-level flag, not a per-tab one. So browsers are pooled
   per proxy rather than shared: a request that needs the CA residential exit
   cannot borrow the direct browser. `None` (direct) is just another pool key.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

import zendriver as zd
from fastapi import FastAPI
from pydantic import BaseModel
from zendriver import cdp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zendriver-service")

HEADLESS = os.getenv("ZENDRIVER_HEADLESS", "false").lower() == "true"
MAX_CONCURRENCY = int(os.getenv("ZENDRIVER_MAX_CONCURRENCY", "4"))
MAX_BROWSERS = int(os.getenv("ZENDRIVER_MAX_BROWSERS", "4"))
CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/chromium")

BASE_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--window-size=1920,1080",
]

app = FastAPI(title="Zendriver Scrape Service")

# proxy key (or None for direct) -> Browser
_browsers: dict[str | None, zd.Browser] = {}
_browser_lock = asyncio.Lock()
_slots = asyncio.Semaphore(MAX_CONCURRENCY)


def _split_proxy(proxy: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (server_for_chrome, username, password).

    `server_for_chrome` deliberately drops any credentials — Chrome rejects a
    --proxy-server value containing them, and they are supplied over CDP instead.
    """
    if not proxy:
        return None, None, None
    parsed = urlparse(proxy)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    if not host:
        return None, None, None
    return f"{scheme}://{host}{port}", parsed.username, parsed.password


async def get_browser(proxy: str | None) -> zd.Browser:
    """One long-lived browser per distinct proxy.

    Browser startup is 1-3s, so this is not per-request. The pool is bounded:
    beyond MAX_BROWSERS the oldest is retired, since an unbounded pool on a
    per-proxy key is an OOM waiting to happen with a rotating pool.
    """
    async with _browser_lock:
        existing = _browsers.get(proxy)
        if existing is not None:
            return existing

        if len(_browsers) >= MAX_BROWSERS:
            oldest_key = next(iter(_browsers))
            oldest = _browsers.pop(oldest_key)
            log.info("retiring browser for proxy=%s (pool full)", oldest_key)
            try:
                await oldest.stop()
            except Exception:
                log.warning("failed stopping retired browser", exc_info=True)

        server, _user, _pwd = _split_proxy(proxy)
        args = list(BASE_BROWSER_ARGS)
        if server:
            args.append(f"--proxy-server={server}")

        browser = await zd.start(
            headless=HEADLESS,
            browser_executable_path=CHROME_BIN,
            browser_args=args,
            # Config only auto-disables the sandbox when running as root; this
            # image runs as a non-root user, so be explicit.
            sandbox=False,
        )
        _browsers[proxy] = browser
        log.info("browser started proxy=%s headless=%s", server or "direct", HEADLESS)
        return browser


async def drop_browser(proxy: str | None) -> None:
    """Discard a browser so the next request rebuilds it. A wedged browser
    otherwise poisons every subsequent request routed to the same proxy."""
    async with _browser_lock:
        browser = _browsers.pop(proxy, None)
    if browser is not None:
        try:
            await browser.stop()
        except Exception:
            log.warning("browser stop failed", exc_info=True)


async def _install_proxy_auth(tab, username: str, password: str) -> None:
    """Answer the proxy's 407 challenge with credentials, over CDP.

    Both handlers are required: enabling the Fetch domain pauses EVERY request,
    so without a RequestPaused handler that resumes them the page never loads.
    """

    async def on_auth(event):
        await tab.send(
            cdp.fetch.continue_with_auth(
                request_id=event.request_id,
                auth_challenge_response=cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials",
                    username=username,
                    password=password,
                ),
            )
        )

    async def on_paused(event):
        await tab.send(cdp.fetch.continue_request(request_id=event.request_id))

    tab.add_handler(cdp.fetch.AuthRequired, on_auth)
    tab.add_handler(cdp.fetch.RequestPaused, on_paused)
    await tab.send(cdp.fetch.enable(handle_auth_requests=True))


class ScrapeRequest(BaseModel):
    url: str
    proxy: str | None = None
    wait_for: str | None = None   # CSS selector: the reliable "it painted" signal
    wait_ms: int = 0              # fallback settle time for pages with no such selector
    timeout: float = 60.0


@app.get("/health")
async def health() -> dict:
    # Deliberately does not touch the browser: this is a liveness probe, and
    # cold-starting Chromium here would make a healthy container look down.
    return {"status": "ok", "headless": HEADLESS, "browsers": len(_browsers)}


@app.post("/scrape")
async def scrape(req: ScrapeRequest) -> dict:
    async with _slots:
        tab = None
        try:
            browser = await get_browser(req.proxy)
            _server, username, password = _split_proxy(req.proxy)

            tab = await browser.get("about:blank", new_tab=True)
            if username and password:
                await _install_proxy_auth(tab, username, password)

            await tab.get(req.url)

            if req.wait_for:
                await tab.select(req.wait_for, timeout=req.timeout)
            else:
                await tab  # let pending CDP events settle
            if req.wait_ms:
                await asyncio.sleep(req.wait_ms / 1000)

            html = await tab.get_content()
            title = await tab.evaluate("document.title") or ""
            final_url = await tab.evaluate("location.href") or req.url

            return {
                "success": True,
                "data": {"title": title, "url": final_url, "html": html},
            }
        except Exception as exc:
            log.warning("scrape failed url=%s: %s", req.url, exc, exc_info=True)
            await drop_browser(req.proxy)
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    pass
