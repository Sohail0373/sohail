# Growvoria Feeds

Multi-tenant Shopify public app that automatically generates **Pinterest Catalog** and **Google Merchant Center**-compatible XML product feeds for any Shopify store that installs it.

Five regional feeds are produced per store, refreshed every 4 hours:

| Region | Path | Currency |
|--------|------|----------|
| United States | `/feed/{shop}/us.xml` | USD |
| United Kingdom | `/feed/{shop}/uk.xml` | GBP |
| UAE | `/feed/{shop}/ae.xml` | AED |
| Saudi Arabia | `/feed/{shop}/sa.xml` | SAR |
| Europe | `/feed/{shop}/eu.xml` | EUR |

---

## Tech Stack

| Concern | Library |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Database | SQLite (dev) / PostgreSQL (prod) via SQLAlchemy |
| Migrations | Alembic |
| Task scheduling | APScheduler (AsyncIOScheduler) |
| XML generation | lxml |
| HTTP client | httpx |
| Retry logic | tenacity |
| Exchange rates | [frankfurter.app](https://www.frankfurter.app) (free, no key) |
| Signed state tokens | itsdangerous |
| Containerisation | Docker |
| Hosting | Koyeb (free tier) |

---

## Prerequisites

- Python 3.12+
- A **Shopify Partner** account → [partners.shopify.com](https://partners.shopify.com)
- (Production) A PostgreSQL database

---

## 1 — Create a Shopify App

1. Log in to the Shopify Partners dashboard.
2. Click **Apps → Create app → Create app manually**.
3. Note the **API key** and **API secret key**.
4. Under **App setup → URLs**, set:
   - **App URL**: `https://app.growvoria.com/install`
   - **Allowed redirection URL**: `https://app.growvoria.com/auth/callback`
5. Under **Configuration → Scopes**, request: `read_products, read_inventory`

---

## 2 — Local Development

```bash
# Clone and enter the repo
git clone https://github.com/yourorg/growvoria-feeds.git
cd growvoria-feeds

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in the environment file
cp .env.example .env
# Edit .env — at minimum set SHOPIFY_API_KEY, SHOPIFY_API_SECRET, SECRET_KEY

# Initialise the database (auto-creates tables via SQLAlchemy)
# Tables are created automatically on startup; or run Alembic explicitly:
alembic upgrade head

# Start the dev server
uvicorn app.main:app --reload --port 8000
```

Visit [http://localhost:8000](http://localhost:8000) — the app is running.

To test OAuth locally, use **ngrok** to expose port 8000:
```bash
ngrok http 8000
# Update APP_URL in .env to the ngrok HTTPS URL
# Update the redirect URL in your Shopify app settings too
```

Then install the app by visiting:
```
http://localhost:8000/install?shop=yourdevstore.myshopify.com
```

---

## 3 — Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SHOPIFY_API_KEY` | ✅ | — | From Shopify Partners |
| `SHOPIFY_API_SECRET` | ✅ | — | From Shopify Partners |
| `SECRET_KEY` | ✅ | — | Random 32-byte hex (`openssl rand -hex 32`) |
| `APP_URL` | ✅ | `https://app.growvoria.com` | Public HTTPS URL of the app |
| `DATABASE_URL` | | `sqlite:///./growvoria_feeds.db` | SQLAlchemy DB URL |
| `FEEDS_DIR` | | `./feeds` | Directory where XML files are stored |
| `SHOPIFY_API_VERSION` | | `2024-10` | Shopify API version |
| `SHOPIFY_SCOPES` | | `read_products,read_inventory` | OAuth permission scopes |
| `FEED_REFRESH_HOURS` | | `4` | How often to refresh all feeds |
| `EXCHANGE_RATE_TTL` | | `3600` | Exchange rate cache TTL in seconds |
| `DEBUG` | | `false` | Enables `/docs` and `/redoc` when true |
| `LOG_LEVEL` | | `INFO` | Python logging level |

---

## 4 — Database Migrations

The app creates tables automatically at startup (`init_db()`). For production, use Alembic:

```bash
# Apply all migrations
alembic upgrade head

# Create a new migration after model changes
alembic revision --autogenerate -m "add new column"
alembic upgrade head
```

---

## 5 — Docker (local)

```bash
# Build and start (SQLite by default)
docker compose up --build

# With PostgreSQL (edit docker-compose.yml to uncomment the db service
# and update DATABASE_URL in .env)
docker compose up --build
```

---

## 6 — Deploy to Koyeb

### One-time setup

1. Push this repo to GitHub.
2. Create a [Koyeb](https://koyeb.com) account (free tier supports 1 service).
3. In the Koyeb dashboard, create **Secrets**:
   - `shopify-api-key`
   - `shopify-api-secret`
   - `database-url` (Koyeb provides a free PostgreSQL add-on)
   - `growvoria-secret-key` (`openssl rand -hex 32`)
4. Create a new **Service** → GitHub → select your repo.
5. Koyeb will detect the `Dockerfile` automatically and use `koyeb.yaml` for config.

### Persistent feed storage on Koyeb

The free Koyeb tier uses an ephemeral filesystem — feeds are lost on restart. To persist them:

- **Option A**: Use Koyeb's [Persistent Volumes](https://www.koyeb.com/docs/run-and-scale/persistent-volumes) (paid tier).
- **Option B**: Store feeds in an S3-compatible object store (e.g. Cloudflare R2 free tier). The `generator.py`'s `_atomic_write` function can be replaced with an upload call.
- **Option C**: Keep SQLite/PostgreSQL and regenerate on startup (feeds are rebuilt within minutes).

For now the app simply regenerates all feeds on restart via the scheduler's first tick.

---

## 7 — Feed URL Reference

After installing, your feed URLs appear on the dashboard. The slug is your shop name without `.myshopify.com`:

```
https://app.growvoria.com/feed/my-store/us.xml   → USD
https://app.growvoria.com/feed/my-store/uk.xml   → GBP
https://app.growvoria.com/feed/my-store/ae.xml   → AED
https://app.growvoria.com/feed/my-store/sa.xml   → SAR
https://app.growvoria.com/feed/my-store/eu.xml   → EUR
```

Feeds are served with `Cache-Control: public, max-age=3600`.

---

## 8 — Feed XML Schema

```xml
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>Store Name — Product Feed</title>
    <link>https://mystore.com</link>
    <item>
      <g:id>123456789</g:id>
      <title>Blue Widget — Large</title>
      <description>Plain-text description (max 5000 chars)</description>
      <link>https://mystore.com/products/blue-widget?variant=123456789</link>
      <g:image_link>https://cdn.shopify.com/…/image.jpg</g:image_link>
      <g:additional_image_link>…</g:additional_image_link>
      <g:availability>in stock</g:availability>
      <g:price>29.99 USD</g:price>
      <g:condition>new</g:condition>
      <g:brand>Acme Corp</g:brand>
      <g:product_type>Widgets</g:product_type>
      <g:item_group_id>987654321</g:item_group_id>
      <g:mpn>SKU-001</g:mpn>
      <g:custom_label_0>sale, featured, new-arrival</g:custom_label_0>
    </item>
  </channel>
</rss>
```

### Item inclusion rules

A variant is **included** when:
- The parent product has a title and at least one image
- The variant `availableForSale = true`
- `inventoryQuantity` is either `null` (untracked) or `> 0`
- `price > 0`

---

## 9 — Architecture

```
Merchant browser
      │
      ▼
GET /install?shop=…
      │
      ▼
Redirect → Shopify OAuth consent
      │
      ▼
GET /auth/callback (code + HMAC verified)
      │
      ├─ Exchange code → access_token
      ├─ Upsert store in DB
      ├─ Register app/uninstalled webhook  ─┐
      └─ Trigger feed generation           ─┘ (background tasks)
              │
              ▼
      ShopifyClient.iter_products()
      (GraphQL, 250/page, tenacity retry)
              │
              ▼
      _build_feed() × 5 currencies
      (lxml, atomic write to disk)
              │
              ▼
      GET /feed/{shop}/{region}.xml
      (served as RSS+XML with 1h cache)

APScheduler: every 4h → regenerate all active stores
```

---

## 10 — Webhooks

| Topic | Endpoint | Action |
|---|---|---|
| `app/uninstalled` | `POST /webhooks/app-uninstalled` | Sets `is_active=False`, clears token |

All webhook requests are verified with HMAC-SHA256 using the Shopify API secret.

---

## License

MIT
