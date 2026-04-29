"""
Vercel Serverless Backend — Python / FastAPI
HTTP and WebSocket integration layer.
"""

import os
import asyncio
import time
import random
import hashlib
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from starlette.responses import StreamingResponse

# ── Config (read once at cold-start) ─────────────────────────

TARGET_BASE = os.environ.get("TARGET_DOMAIN", "").rstrip("/")
COVER_NAME = os.environ.get("COVER_NAME", "Nimbus Systems")
COVER_TAGLINE = os.environ.get("COVER_TAGLINE", "Next-Generation Cloud Infrastructure")

_STRIP_REQ = frozenset({
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "forwarded", "x-forwarded-host", "x-forwarded-proto",
    "x-forwarded-port",
})

_STRIP_RESP = frozenset({
    "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
})

_BOOT = int(time.time())
_ETAG = hashlib.md5(f"{_BOOT}{random.random()}".encode()).hexdigest()[:12]

# ── Shared async HTTP client (pooled across warm invocations) ─

_http = httpx.AsyncClient(
    follow_redirects=False,
    timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
    http2=True,
)

# ── App ──────────────────────────────────────────────────────

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _stealth(headers: dict) -> dict:
    """Apply standard server response headers."""
    headers["server"] = "nginx/1.24.0"
    headers["x-content-type-options"] = "nosniff"
    headers["x-frame-options"] = "SAMEORIGIN"
    headers["referrer-policy"] = "strict-origin-when-cross-origin"
    headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
    headers.pop("x-powered-by", None)
    return headers


# ── Static endpoints ─────────────────────────────────────────

@app.get("/")
async def cover_page(request: Request):
    h = _stealth({"cache-control": "public, max-age=3600", "etag": f'"{_ETAG}"'})
    if request.headers.get("if-none-match") == f'"{_ETAG}"':
        return Response(status_code=304, headers=h)
    return HTMLResponse(content=_cover_html(), headers=h)


@app.head("/")
async def cover_head():
    return Response(
        status_code=200,
        headers=_stealth({"content-type": "text/html; charset=utf-8"}),
    )


@app.get("/robots.txt")
async def robots_txt():
    body = "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /cdn-cgi/\n"
    return Response(content=body, media_type="text/plain",
                    headers=_stealth({"cache-control": "public, max-age=86400"}))


@app.get("/favicon.ico")
async def favicon():
    # 1x1 transparent ICO
    ico = (b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00"
           b"\x30\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00"
           b"\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00"
           b"\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
           b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    return Response(content=ico, media_type="image/x-icon",
                    headers=_stealth({"cache-control": "public, max-age=604800"}))


@app.get("/sitemap.xml")
async def sitemap(request: Request):
    host = request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto", "https")
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f'  <url><loc>{scheme}://{host}/</loc><priority>1.0</priority></url>\n'
            f'</urlset>\n')
    return Response(content=body, media_type="application/xml",
                    headers=_stealth({"cache-control": "public, max-age=86400"}))


# ── WebSocket handler ────────────────────────────────────────

@app.websocket("/{path:path}")
async def ws_handler(ws: WebSocket, path: str):
    if not TARGET_BASE:
        await ws.close(code=1011, reason="misconfigured")
        return

    parsed = urlparse(TARGET_BASE)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    target_ws = f"{ws_scheme}://{parsed.netloc}/{path}"
    if ws.scope.get("query_string"):
        target_ws += "?" + ws.scope["query_string"].decode()

    await ws.accept()

    import websockets.client as wsc

    try:
        extra_headers = {}
        for k, v in ws.headers.items():
            lk = k.lower()
            if lk in _STRIP_REQ or lk.startswith("x-vercel-") or lk.startswith("x-forwarded-"):
                continue
            if lk in ("sec-websocket-key", "sec-websocket-version",
                       "sec-websocket-extensions", "sec-websocket-protocol"):
                continue
            extra_headers[k] = v

        client_ip = ws.headers.get("x-real-ip") or ws.headers.get("x-forwarded-for", "")
        if client_ip:
            extra_headers["x-forwarded-for"] = client_ip.split(",")[0].strip()

        async with wsc.connect(
            target_ws,
            additional_headers=extra_headers,
            close_timeout=5,
            open_timeout=10,
        ) as upstream:
            done = asyncio.Event()

            async def c2u():
                try:
                    while not done.is_set():
                        msg = await ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass
                finally:
                    done.set()

            async def u2c():
                try:
                    async for frame in upstream:
                        if done.is_set():
                            break
                        if isinstance(frame, str):
                            await ws.send_text(frame)
                        else:
                            await ws.send_bytes(frame)
                except Exception:
                    pass
                finally:
                    done.set()

            await asyncio.gather(c2u(), u2c())
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ── HTTP handler (catch-all) ─────────────────────────────────

@app.api_route("/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def http_handler(request: Request, path: str):
    if not TARGET_BASE:
        return Response("Service Unavailable", status_code=503,
                        headers=_stealth({}))

    target_url = f"{TARGET_BASE}/{path}"
    qs = str(request.query_params)
    if qs:
        target_url += f"?{qs}"

    # Build outgoing headers
    out_headers = {}
    client_ip = None
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _STRIP_REQ or lk.startswith("x-vercel-"):
            continue
        if lk == "x-real-ip":
            client_ip = v
            continue
        if lk == "x-forwarded-for":
            if not client_ip:
                client_ip = v
            continue
        out_headers[k] = v
    if client_ip:
        out_headers["x-forwarded-for"] = client_ip.split(",")[0].strip()

    method = request.method
    has_body = method not in ("GET", "HEAD", "OPTIONS")

    try:
        upstream = await _http.request(
            method=method,
            url=target_url,
            headers=out_headers,
            content=request.stream() if has_body else None,
        )

        # Normalize response headers
        resp_headers = {}
        for k, v in upstream.headers.multi_items():
            if k.lower() in _STRIP_RESP:
                continue
            resp_headers[k] = v
        _stealth(resp_headers)

        # Stream response body
        async def body_stream():
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                yield chunk
            await upstream.aclose()

        return StreamingResponse(
            content=body_stream(),
            status_code=upstream.status_code,
            headers=resp_headers,
        )
    except httpx.TimeoutException:
        return Response("Gateway Timeout", status_code=504,
                        headers=_stealth({"content-type": "text/plain"}))
    except Exception:
        return Response("Bad Gateway", status_code=502,
                        headers=_stealth({"content-type": "text/plain"}))


# ── Landing page HTML ────────────────────────────────────────

def _cover_html() -> str:
    year = time.strftime("%Y")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{COVER_NAME} — {COVER_TAGLINE}</title>
<meta name="description" content="{COVER_NAME} provides scalable, secure cloud infrastructure for modern enterprises. Deploy globally in seconds.">
<meta name="theme-color" content="#0a0e1a">
<link rel="icon" href="/favicon.ico">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0e1a;--surface:#111827;--border:#1e293b;--text:#e2e8f0;
--muted:#94a3b8;--accent:#6366f1;--accent2:#818cf8;--glow:rgba(99,102,241,.15)}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased;
overflow-x:hidden}}
a{{color:var(--accent2);text-decoration:none}}
a:hover{{text-decoration:underline}}
.container{{max-width:1100px;margin:0 auto;padding:0 24px}}

/* NAV */
nav{{position:sticky;top:0;z-index:100;background:rgba(10,14,26,.85);
backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:16px 0}}
nav .container{{display:flex;align-items:center;justify-content:space-between}}
.logo{{font-size:1.25rem;font-weight:700;letter-spacing:-.02em;
background:linear-gradient(135deg,var(--accent),var(--accent2));
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
nav ul{{list-style:none;display:flex;gap:28px}}
nav li a{{color:var(--muted);font-size:.875rem;transition:color .2s}}
nav li a:hover{{color:var(--text);text-decoration:none}}
.nav-cta{{padding:8px 18px;background:var(--accent);color:#fff;border-radius:8px;
font-size:.875rem;font-weight:600;transition:background .2s}}
.nav-cta:hover{{background:var(--accent2);text-decoration:none}}

/* HERO */
.hero{{text-align:center;padding:120px 0 80px;position:relative}}
.hero::before{{content:"";position:absolute;top:0;left:50%;transform:translateX(-50%);
width:600px;height:600px;background:radial-gradient(circle,var(--glow) 0%,transparent 70%);
pointer-events:none}}
.hero h1{{font-size:clamp(2.2rem,5vw,3.5rem);font-weight:800;letter-spacing:-.03em;
line-height:1.15;margin-bottom:20px;position:relative}}
.hero h1 span{{background:linear-gradient(135deg,var(--accent),#a78bfa,var(--accent2));
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hero p{{color:var(--muted);font-size:1.125rem;max-width:560px;margin:0 auto 36px}}
.hero-buttons{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}}
.btn{{padding:12px 28px;border-radius:10px;font-weight:600;font-size:.95rem;
transition:all .25s;cursor:pointer;border:none}}
.btn-primary{{background:var(--accent);color:#fff;box-shadow:0 4px 20px rgba(99,102,241,.35)}}
.btn-primary:hover{{background:var(--accent2);transform:translateY(-1px);text-decoration:none;color:#fff}}
.btn-outline{{background:transparent;color:var(--text);border:1px solid var(--border)}}
.btn-outline:hover{{border-color:var(--accent);text-decoration:none}}

/* METRICS */
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:20px;
padding:60px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}}
.metric{{text-align:center}}
.metric .num{{font-size:2rem;font-weight:800;
background:linear-gradient(135deg,var(--accent),var(--accent2));
-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.metric .label{{color:var(--muted);font-size:.85rem;margin-top:4px}}

/* FEATURES */
.features{{padding:80px 0}}
.features h2{{font-size:1.8rem;font-weight:700;text-align:center;margin-bottom:48px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;
padding:28px;transition:border-color .3s,transform .3s}}
.card:hover{{border-color:var(--accent);transform:translateY(-3px)}}
.card-icon{{width:44px;height:44px;border-radius:10px;background:var(--glow);
display:flex;align-items:center;justify-content:center;margin-bottom:16px;font-size:1.3rem}}
.card h3{{font-size:1.05rem;font-weight:600;margin-bottom:8px}}
.card p{{color:var(--muted);font-size:.9rem;line-height:1.65}}

/* FOOTER */
footer{{border-top:1px solid var(--border);padding:40px 0;text-align:center;
color:var(--muted);font-size:.825rem}}
footer a{{color:var(--muted)}}
footer a:hover{{color:var(--text)}}

@media(max-width:640px){{
  nav ul{{display:none}}
  .hero{{padding:80px 0 50px}}
  .metrics{{grid-template-columns:repeat(2,1fr)}}
}}
</style>
</head>
<body>
<nav><div class="container">
  <div class="logo">{COVER_NAME}</div>
  <ul>
    <li><a href="#">Platform</a></li>
    <li><a href="#">Solutions</a></li>
    <li><a href="#">Pricing</a></li>
    <li><a href="#">Docs</a></li>
    <li><a href="#">Blog</a></li>
  </ul>
  <a href="#" class="nav-cta">Get Started</a>
</div></nav>

<main>
<section class="hero"><div class="container">
  <h1>Deploy at the<br><span>Speed of Light</span></h1>
  <p>Scalable compute, storage, and networking — all in one platform.
     Ship faster with zero-downtime deployments across 42 global edge regions.</p>
  <div class="hero-buttons">
    <a href="#" class="btn btn-primary">Start Building — Free</a>
    <a href="#" class="btn btn-outline">View Documentation</a>
  </div>
</div></section>

<section class="container">
<div class="metrics">
  <div class="metric"><div class="num">99.99%</div><div class="label">Uptime SLA</div></div>
  <div class="metric"><div class="num">42</div><div class="label">Edge Regions</div></div>
  <div class="metric"><div class="num">&lt;18ms</div><div class="label">Avg Latency</div></div>
  <div class="metric"><div class="num">12M+</div><div class="label">Deployments / mo</div></div>
</div>
</section>

<section class="features"><div class="container">
  <h2>Everything you need to scale</h2>
  <div class="grid">
    <div class="card">
      <div class="card-icon">&#9889;</div>
      <h3>Instant Deployments</h3>
      <p>Push to deploy in under 3 seconds. Atomic rollbacks, preview URLs, and branch-based environments included.</p>
    </div>
    <div class="card">
      <div class="card-icon">&#128274;</div>
      <h3>Zero-Trust Security</h3>
      <p>mTLS everywhere, automated certificate management, WAF, and DDoS mitigation at the edge — no config required.</p>
    </div>
    <div class="card">
      <div class="card-icon">&#127758;</div>
      <h3>Global Edge Network</h3>
      <p>Serve content from the closest node. Intelligent routing, automatic failover, and real-time analytics built in.</p>
    </div>
    <div class="card">
      <div class="card-icon">&#128200;</div>
      <h3>Auto Scaling</h3>
      <p>Scale from zero to millions of requests without lifting a finger. Pay only for what you use, down to the millisecond.</p>
    </div>
    <div class="card">
      <div class="card-icon">&#128640;</div>
      <h3>Managed Databases</h3>
      <p>Postgres, Redis, and object storage — fully managed, replicated, and optimized for edge-first architectures.</p>
    </div>
    <div class="card">
      <div class="card-icon">&#129302;</div>
      <h3>AI-Native Tooling</h3>
      <p>First-class support for inference endpoints, vector stores, and model routing. Build AI apps without the infra headache.</p>
    </div>
  </div>
</div></section>
</main>

<footer><div class="container">
  <p>&copy; {year} {COVER_NAME}, Inc. &nbsp;·&nbsp;
     <a href="#">Terms</a> &nbsp;·&nbsp;
     <a href="#">Privacy</a> &nbsp;·&nbsp;
     <a href="#">Status</a></p>
</div></footer>
</body>
</html>"""
