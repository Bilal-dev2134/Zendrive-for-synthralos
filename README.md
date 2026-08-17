# Zendriver scrape service

The VPS side of SynthralOS's `zendriver` scraping engine. Wraps
[Zendriver](https://zendriver.dev) — an async, CDP-driven undetectable browser — in a
small FastAPI service, because Zendriver is a library with no server of its own.

Deployed on Coolify, **port 8895**. Consumed by
`app.scraping.services.vps_client.fetch_zendriver` in the SynthralOS backend.

## Contract

```
POST /scrape
{
  "url":      "https://example.com",
  "proxy":    "http://user:pass@host:port",   // optional
  "wait_for": ".result-row",                  // optional CSS selector
  "wait_ms":  0,                              // optional settle time
  "timeout":  60.0
}

200 {"success": true,  "data": {"title": "...", "url": "...", "html": "..."}}
200 {"success": false, "error": "..."}

GET /health -> {"status": "ok", "headless": false, "browsers": 0}
```

Failures return HTTP 200 with `success: false` — clients read `success`, not the status
code. `/health` deliberately does not start a browser, so a cold container doesn't read
as unhealthy.

## Deploy (Coolify)

- **Build Pack: Docker Compose** — not Dockerfile. Only the Compose pack applies
  `shm_size`, and without it Chromium dies with `DevToolsActivePort file doesn't exist`.
- Compose location `/docker-compose.yml`.
- Open port 8895 on the VPS firewall.

| Env var | Default | Notes |
|---|---|---|
| `ZENDRIVER_HEADLESS` | `false` | Keep false. Chrome runs non-headless under Xvfb; headless is the most fingerprinted signal there is |
| `ZENDRIVER_MAX_CONCURRENCY` | `4` | Concurrent tabs |
| `ZENDRIVER_MAX_BROWSERS` | `4` | Browser pool cap — one browser per distinct proxy |

All are interpolated in `docker-compose.yml`, so Coolify's Environment Variables tab can
override them without editing the file.

## Two things that are easy to get wrong

1. **Chrome ignores credentials in `--proxy-server`.** An authenticated residential proxy
   must be answered over CDP: `Fetch.enable(handleAuthRequests=True)` plus handlers for
   `Fetch.authRequired` *and* `Fetch.requestPaused`. The second is not optional —
   enabling the Fetch domain pauses every request, so without a handler that resumes
   them the page never loads at all.
2. **The proxy is a browser-level flag, not per-tab.** Browsers are pooled per proxy;
   `None` (direct) is just another pool key.

## Smoke tests

```bash
curl http://<VPS_IP>:8895/health

curl -X POST http://<VPS_IP>:8895/scrape -H 'Content-Type: application/json' \
  -d '{"url":"https://www.browserscan.net/bot-detection"}' | head -c 400

# The decisive one: proxy auth actually firing
curl -X POST http://<VPS_IP>:8895/scrape -H 'Content-Type: application/json' \
  -d '{"url":"https://api.ipify.org?format=json","proxy":"http://user:pass@host:port"}'
```

The third **must** report the proxy's IP, not the VPS's. If it reports the VPS IP the CDP
auth handler is not firing, and every WAF-protected origin will still fail.

## Notes

- `requirements.txt` is intentionally unpinned for the first build. Run `pip freeze` in
  the container afterwards and pin — a Zendriver bump can change the CDP surface the
  proxy-auth handler depends on.
- `.gitattributes` forces LF. The Dockerfile uses backslash line-continuations; CRLF
  breaks them in a way whose error points nowhere near the cause.

Full reference, including the backend wiring and portal-ladder placement:
`backend/docs/ZENDRIVER_REFERENCE.md` in the SynthralOS repo.
