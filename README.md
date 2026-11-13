# Automate Inventory Monitoring with Python

A complete, production-grade inventory monitoring, anomaly detection, replenishment planning, and multi-channel alerting system built with **Python 3.11+**, **SQLAlchemy 2.x**, **FastAPI**, **Pydantic v2**, **APScheduler**, and **Streamlit + Plotly**.

---

## Architecture Diagram

```mermaid
flowchart LR
    subgraph Sources["External Data Sources"]
        CSV["CSV Upload / CLI"]
        Watch["Folder-Watch CSV Drop"]
        ERP["ERP / Google Sheets Adapter"]
    end

    subgraph Core["Application & Domain Services"]
        Importer["CSV Importer & Validator"]
        InvSvc["InventoryService"]
        Engine["MonitoringEngine\n(Rules, Forecast, Replenishment)"]
        Sched["APScheduler\n(Periodic Check & Daily Report)"]
        Dispatch["NotificationDispatcher\n(Throttle, Quiet Hours, Retry)"]
    end

    subgraph Storage["Persistence Layer"]
        DB[("SQLite / PostgreSQL\nSQLAlchemy 2.x ORM")]
    end

    subgraph Interfaces["User & API Interfaces"]
        API["FastAPI REST API\n(X-API-Key + Rate Limit)"]
        UI["Streamlit + Plotly\nWeb Dashboard"]
    end

    subgraph Channels["Notification Channels"]
        Email["SMTP HTML Email"]
        TG["Telegram Bot"]
        Slack["Slack Webhook"]
        Console["Rotating Console/File Log"]
    end

    CSV --> Importer
    Watch --> Importer
    ERP --> Importer
    Importer --> DB
    Sched --> Engine
    Engine <--> DB
    Engine --> Dispatch
    Dispatch --> Email
    Dispatch --> TG
    Dispatch --> Slack
    Dispatch --> Console
    API <--> InvSvc
    UI <--> InvSvc
    InvSvc <--> DB
    InvSvc --> Engine
```

---

## Key Features

1. **Data Model & Validated CSV Ingestion (Phase 1)**
   - SQLAlchemy 2.x ORM models (`Product`, `Warehouse`, `StockLevel`, `StockMovement`, `Alert`, `AlertSetting`, `NotificationLog`, `SystemMetadata`).
   - Hybrid `available = quantity_on_hand - quantity_reserved` property usable in Python and SQL expressions.
   - Row-level CSV validation for products and stock levels with detailed per-row error reporting.
   - Realistic seed generator (`scripts/seed.py`): 50 products across 4 categories, 3 warehouses, and 90 days of movement history (~2,565 ledger entries).

2. **Framework-Independent Monitoring Engine (Phase 2)**
   - Detects and classifies 6 alert rules:
     - `OUT_OF_STOCK` (`available <= 0`) — **CRITICAL**
     - `LOW_STOCK` (`0 < available <= reorder_point`) — **WARNING**
     - `NEGATIVE_STOCK` (`quantity_on_hand < 0` or `quantity_reserved > quantity_on_hand`) — **CRITICAL** data-quality anomaly
     - `OVERSTOCK` (`available > overstock_threshold`) — **WARNING**
     - `SLOW_MOVING` (no movement in `N` days with `available > 0`) — **WARNING**
     - `STOCKOUT_FORECAST` (estimated days of stock remaining from 30-day average daily consumption `<= forecast_warning_days`) — **WARNING / CRITICAL**
   - **Duplicate Alert Prevention**: Never re-creates an open alert for the same `(product_id, warehouse_id, type)`.
   - **Auto-Resolution**: Automatically resolves open alerts when stock recovers.
   - **Replenishment Engine**: Calculates recommended order quantities per product, groups by supplier, and exports purchase schedules as CSV.

3. **Automation & Resilient Notifications (Phase 3)**
   - Scheduled monitoring checks every `N` minutes and a daily HTML/text summary report via **APScheduler**.
   - Pluggable notification channels (`EmailChannel`, `TelegramChannel`, `SlackChannel`, `ConsoleChannel`) controlled via `.env`.
   - Cooldown throttling, quiet-hours suppression (with critical alert bypass), exponential backoff retry, and database audit logging (`NotificationLog`).
   - `InventorySource` adapter interface with a working `CSVFolderWatchSource` (`data/watch/`).

4. **FastAPI REST API (Phase 4)**
   - `X-API-Key` header authentication, sliding-window rate limiting (`429 Too Many Requests`), CORS configuration, and Pydantic v2 schemas.
   - Endpoints: `GET /products`, `POST /products`, `GET /stock`, `POST /stock/movements`, `GET /stock/movements`, `GET /alerts`, `POST /alerts/{id}/acknowledge`, `GET/PUT /settings/thresholds`, `GET /summary`, `GET /replenishment`, `GET /health`, `POST /import/csv`.

5. **Streamlit + Plotly Web Dashboard (Phase 5)**
   - 6 interactive views: **Dashboard**, **Inventory**, **Alerts**, **Replenishment**, **Movements**, **Settings**.
   - KPI cards, warehouse donut chart, category horizontal bar chart, stock trend line chart with date range selector, low-stock panel, activity feed, quick actions, and system status bar.

---

## Screenshots (Placeholders)

| View | Screenshot Placeholder |
| :--- | :--- |
| **Main Operations Dashboard** | `![Dashboard KPIs & Charts](docs/screenshots/dashboard_overview.png)` |
| **Inventory Explorer & CSV Import** | `![Inventory Explorer](docs/screenshots/inventory_table.png)` |
| **Alerts & Anomaly Management** | `![Alerts Management](docs/screenshots/alerts_panel.png)` |
| **Supplier Replenishment Planner** | `![Replenishment Planner](docs/screenshots/replenishment_by_supplier.png)` |

---

## Local Setup & Execution

### 1. Install Dependencies & Configure Environment
```bash
cp .env.example .env
pip install -r requirements.txt
```

### 2. Seed Sample Data & Run Initial Monitoring Check
```bash
python3 -m scripts.seed
python3 -m scripts.run_scheduler --once
```

### 3. Import Products or Stock Levels via CLI
```bash
python3 -m scripts.import_csv --type products --file data/sample_imports/sample_products.csv
python3 -m scripts.import_csv --type stock --file data/sample_imports/sample_stock_levels.csv
```

### 4. Start the FastAPI Server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
- Interactive Swagger UI: `http://localhost:8000/docs`
- Health Check: `curl http://localhost:8000/health`
- Authenticated Example:
  ```bash
  curl -H "X-API-Key: inv-dev-secret-api-key-2025" http://localhost:8000/summary
  ```

### 5. Start the Background Scheduler Daemon
```bash
python -m scripts.run_scheduler
```

### 6. Launch the Streamlit Dashboard
```bash
streamlit run dashboard/app.py
```
- Open `http://localhost:8501` in your browser.

### 7. Run the Test Suite
```bash
pytest
```

---

## Running with Docker & Docker Compose

### SQLite (Default)
```bash
docker compose up --build
```
- **FastAPI**: `http://localhost:8000` (`http://localhost:8000/docs`)
- **Streamlit Dashboard**: `http://localhost:8501`

### With PostgreSQL 16
```bash
DATABASE_URL=postgresql+psycopg2://inventory_user:change_me@postgres:5432/inventory_db \
  docker compose --profile postgres up --build
```

---

## How to Extend

### Adding a New ERP or Cloud Source (e.g., Odoo or Google Sheets)
Subclass `InventorySource` in `app/sources/base.py`:
```python
from sqlalchemy.orm import Session
from app.sources.base import InventorySource, SourceSyncResult

class OdooInventorySource(InventorySource):
    @property
    def source_name(self) -> str:
        return "odoo_erp"

    def sync(self, session: Session) -> SourceSyncResult:
        # Fetch products & stock quants via Odoo XML-RPC / JSON-RPC
        # Upsert via import_products_csv or InventoryService
        return SourceSyncResult(source_name=self.source_name)
```

### Adding a New Notification Channel (e.g., PagerDuty or Microsoft Teams)
Subclass `NotificationChannel` in `app/services/notifications/base.py` and register the instance in `NotificationDispatcher`.

### Adding a New Monitoring Rule
Add a pure detector function `(snapshot: StockSnapshot, thresholds: ThresholdConfig) -> AlertCandidate | None` in `app/services/monitoring/rules.py` and include it in `evaluate_snapshot()`.

