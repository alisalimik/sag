# vercel-backend

A **Vercel Python serverless function** that provides an HTTP and WebSocket
integration layer. Built with FastAPI, it serves a landing page on the root
path and connects to a configurable backend origin for all other routes.

> **Stack:** FastAPI · httpx (HTTP/2) · websockets · Vercel Serverless

## Features

| Feature | Details |
|---|---|
| **HTTP streaming** | Async bidirectional body streaming via `httpx` with HTTP/2 |
| **WebSocket** | Full binary/text frame handling via `websockets` |
| **Landing page** | Clean, responsive page served on `/` with proper caching |
| **SEO & bots** | `/robots.txt`, `/favicon.ico`, `/sitemap.xml` included |
| **Security headers** | HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy |
| **Configurable** | Site branding and backend origin set via environment variables |

## Architecture

```
┌──────────┐   TLS (Vercel cert)   ┌─────────────────┐   HTTP/2 or WS   ┌──────────────┐
│  Client   │ ───────────────────► │  Vercel Python   │ ───────────────► │   Backend    │
│           │                      │  (FastAPI)       │   streaming      │   Origin     │
└──────────┘                       └─────────────────┘                   └──────────────┘
```

1. Client connects to your Vercel domain.
2. TLS terminates at Vercel's edge using their certificate.
3. `/` returns the landing page; all other paths are handled by the backend origin.
4. Request and response bodies are streamed with zero buffering.

## Setup

### 1. Requirements

- A backend origin server reachable over HTTPS (or HTTP).
- [Vercel CLI](https://vercel.com/docs/cli): `npm i -g vercel`
- A Vercel account.

### 2. Environment Variables

In Vercel Dashboard → Project → **Settings → Environment Variables**:

| Name | Example | Description |
|---|---|---|
| `TARGET_DOMAIN` | `https://backend.example.com:443` | Full URL of your backend origin |
| `COVER_NAME` | `Nimbus Systems` | Brand name shown on landing page (optional) |
| `COVER_TAGLINE` | `Next-Gen Cloud` | Tagline shown on landing page (optional) |

Notes:
- Use `https://` or `http://` depending on your backend.
- Include a non-default port if needed.
- Trailing slashes are stripped automatically.

### 3. Deploy

```bash
git clone <your-repo>
cd vercel-backend
vercel --prod
```

After deployment you get a URL like `your-app.vercel.app`.

## Configuration Examples

### Custom domain

Attach a custom domain in the Vercel dashboard → **Domains**. The function
handles all traffic automatically regardless of hostname.

### WebSocket path

WebSocket connections to any path (e.g., `wss://your-app.vercel.app/ws`)
are handled and connected to the corresponding path on your backend.

### HTTP path

All HTTP methods (GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD) on any
path are streamed to/from the backend. Query strings are preserved.

## Project Layout

```
.
├── api/index.py       # FastAPI handler: landing page + HTTP + WebSocket
├── requirements.txt   # Python deps (fastapi, httpx, websockets)
├── vercel.json        # Routes all paths → handler
└── README.md
```

## Performance Notes

- `TARGET_DOMAIN` is read once at cold-start and cached at module scope.
- `httpx.AsyncClient` with HTTP/2 and connection pooling is reused across warm invocations.
- Response bodies are streamed in 64KB chunks — no buffering.
- Headers are filtered in a single pass.

## License

MIT.
