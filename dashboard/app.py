"""Streamlit + Plotly web dashboard for Automate Inventory Monitoring with Python."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import desc, func, select

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT_STR = str(PROJECT_ROOT)
if PROJECT_ROOT_STR in sys.path:
    sys.path.remove(PROJECT_ROOT_STR)
sys.path.insert(0, PROJECT_ROOT_STR)

from app.core.config import get_settings
from app.core.database import SessionLocal, init_db
from app.core.exceptions import InventoryError
from app.models.entities import NotificationLog, Product, utc_now
from app.models.enums import AlertSeverity, AlertType, MovementReason, StockStatus
from app.models.schemas import (
    ProductCreate,
    StockMovementCreate,
    ThresholdSettingUpdate,
)
from app.services.importer import import_products_csv, import_stock_levels_csv
from app.services.inventory_service import InventoryService
from app.services.monitoring.engine import MonitoringEngine
from app.services.notifications.base import NotificationPayload
from app.services.notifications.dispatcher import NotificationDispatcher
from app.sources.csv_watch import CSVFolderWatchSource
from scripts.seed import seed_database

# Semantic Color Palette
COLOR_HEALTHY = "#10b981"  # Green
COLOR_LOW = "#f59e0b"      # Orange
COLOR_CRITICAL = "#ef4444" # Red
COLOR_PRIMARY = "#2563eb"  # Blue
COLOR_NEUTRAL = "#64748b"  # Slate

NAVIGATION_PAGES = [
    "Dashboard",
    "Inventory",
    "Alerts",
    "Replenishment",
    "Movements",
    "Settings",
]


def inject_custom_css() -> None:
    """Inject modern light-theme CSS styles for KPI cards, badges, and status bar."""
    st.markdown(
        """
        <style>
        .stApp {
            background-color: #f8fafc;
        }
        .status-bar {
            display: flex;
            flex-wrap: wrap;
            gap: 18px;
            align-items: center;
            justify-content: space-between;
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 10px 18px;
            margin-bottom: 18px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
            font-size: 0.88rem;
            color: #334155;
        }
        .kpi-card {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 18px 20px;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
            height: 100%;
        }
        .kpi-label {
            font-size: 0.82rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: #64748b;
            margin-bottom: 6px;
        }
        .kpi-value {
            font-size: 1.75rem;
            font-weight: 700;
            color: #0f172a;
            line-height: 1.2;
        }
        .kpi-sub {
            font-size: 0.78rem;
            margin-top: 6px;
            font-weight: 500;
        }
        .badge {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        .badge-healthy { background: #d1fae5; color: #065f46; }
        .badge-low { background: #fef3c7; color: #92400e; }
        .badge-critical { background: #fee2e2; color: #991b1b; }
        .badge-neutral { background: #e2e8f0; color: #334155; }
        .empty-state {
            background: #ffffff;
            border: 1px dashed #cbd5e1;
            border-radius: 12px;
            padding: 32px 20px;
            text-align: center;
            color: #64748b;
            margin: 12px 0;
        }
        .activity-item {
            background: #ffffff;
            border-left: 4px solid #cbd5e1;
            border-radius: 6px;
            padding: 10px 14px;
            margin-bottom: 8px;
            border-top: 1px solid #f1f5f9;
            border-right: 1px solid #f1f5f9;
            border-bottom: 1px solid #f1f5f9;
            font-size: 0.88rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_empty_state(title: str, subtitle: str) -> None:
    """Display a clean empty-state card when a query or filter yields no records."""
    st.markdown(
        f"""
        <div class="empty-state">
            <h4 style="margin:0 0 6px 0; color:#334155;">{title}</h4>
            <p style="margin:0; font-size:0.9rem;">{subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def ensure_seeded_and_checked() -> None:
    """Initialize schema and auto-seed sample data if the database is empty."""
    init_db()
    with SessionLocal() as session:
        count = session.scalar(select(func.count(Product.id))) or 0
        if count == 0:
            seed_database(session, reset=False)
            MonitoringEngine(session).run_check()


@st.cache_data(ttl=15, show_spinner=False)
def cached_dashboard_summary() -> dict[str, Any]:
    """Fetch and cache top-level dashboard KPI summary and chart breakdowns."""
    with SessionLocal() as session:
        service = InventoryService(session)
        return service.get_dashboard_summary().model_dump()


@st.cache_data(ttl=15, show_spinner=False)
def cached_stock_trend(start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Fetch and cache historical daily stock trend series."""
    start_dt = datetime.fromisoformat(start_iso)
    end_dt = datetime.fromisoformat(end_iso)
    with SessionLocal() as session:
        service = InventoryService(session)
        return service.get_stock_trend_series(start_date=start_dt, end_date=end_dt)


@st.cache_data(ttl=15, show_spinner=False)
def cached_replenishment_data() -> tuple[dict[str, Any], str]:
    """Fetch and cache replenishment report and CSV export string."""
    with SessionLocal() as session:
        engine = MonitoringEngine(session)
        report = engine.generate_replenishment_report()
        csv_str = engine.export_replenishment_to_csv()
        return report.model_dump(), csv_str


def invalidate_caches() -> None:
    """Clear all cached dashboard queries after a state mutation."""
    st.cache_data.clear()


def render_system_status_bar() -> None:
    """Render the top System Status bar showing DB connectivity, last sync, and scheduler status."""
    with SessionLocal() as session:
        health = InventoryService(session).get_system_health()

    db_badge = (
        '<span class="badge badge-healthy">Connected</span>'
        if health.database_connected
        else '<span class="badge badge-critical">Disconnected</span>'
    )
    sched_cls = (
        "badge-healthy"
        if health.scheduler_status in {"RUNNING", "READY"}
        else "badge-low"
    )
    sched_badge = f'<span class="badge {sched_cls}">{health.scheduler_status}</span>'
    last_check = (
        health.last_monitoring_run[:19].replace("T", " ") + " UTC"
        if health.last_monitoring_run
        else "Not run yet"
    )
    last_sync = (
        health.last_source_sync[:19].replace("T", " ") + " UTC"
        if health.last_source_sync
        else "N/A"
    )

    st.markdown(
        f"""
        <div class="status-bar">
            <div><strong>Database:</strong> {db_badge}</div>
            <div><strong>Last Monitoring Check:</strong> <code>{last_check}</code></div>
            <div><strong>Last Source Sync:</strong> <code>{last_sync}</code></div>
            <div><strong>Scheduler:</strong> {sched_badge}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_kpi_card(
    label: str,
    value: str,
    subtext: str,
    accent_color: str = COLOR_PRIMARY,
) -> None:
    """Render a styled KPI metric card with semantic accent color."""
    st.markdown(
        f"""
        <div class="kpi-card" style="border-top: 4px solid {accent_color};">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value">{value}</div>
            <div class="kpi-sub" style="color: {accent_color};">{subtext}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard_page() -> None:
    """Render the main Dashboard page: KPIs, Quick Actions, Low Stock Alerts, Charts, and Activity."""
    st.title("Inventory Operations Dashboard")
    render_system_status_bar()

    summary = cached_dashboard_summary()
    _, repl_csv = cached_replenishment_data()

    # 1. KPI Cards Row
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        render_kpi_card(
            "Total Products",
            f"{summary['total_products']:,}",
            f"Across {summary['total_warehouses']} warehouses",
            COLOR_PRIMARY,
        )
    with c2:
        render_kpi_card(
            "Total Stock Units",
            f"{summary['total_stock_units']:,}",
            f"{summary['total_available_units']:,} available net",
            COLOR_HEALTHY,
        )
    with c3:
        render_kpi_card(
            "Low Stock Items",
            f"{summary['low_stock_items']:,}",
            "At or below reorder point",
            COLOR_LOW if summary["low_stock_items"] > 0 else COLOR_HEALTHY,
        )
    with c4:
        render_kpi_card(
            "Out of Stock",
            f"{summary['out_of_stock_items']:,}",
            "Immediate action required" if summary["out_of_stock_items"] > 0 else "All stocked",
            COLOR_CRITICAL if summary["out_of_stock_items"] > 0 else COLOR_HEALTHY,
        )
    with c5:
        render_kpi_card(
            "Total Stock Value",
            f"${summary['total_stock_value']:,.2f}",
            f"{summary['active_alerts_count']} open alerts",
            COLOR_PRIMARY,
        )

    st.write("")

    # 2. Quick Actions Row
    st.subheader("Quick Actions")
    qa1, qa2, qa3, qa4 = st.columns(4)
    with qa1:
        with st.popover("➕ Add Product", width="stretch"):
            with st.form("quick_add_product_form", clear_on_submit=True):
                sku = st.text_input("SKU *", placeholder="ELC-099")
                name = st.text_input("Product Name *", placeholder="Industrial Router")
                category = st.selectbox(
                    "Category",
                    [
                        "Electronics",
                        "Industrial Hardware",
                        "Office Supplies",
                        "Packaging & Logistics",
                    ],
                )
                supplier = st.text_input("Supplier *", placeholder="Apex Components Ltd")
                unit_cost = st.number_input("Unit Cost ($)", min_value=0.0, value=45.0, step=1.0)
                rop = st.number_input("Reorder Point", min_value=0, value=15, step=1)
                roq = st.number_input("Reorder Quantity", min_value=1, value=50, step=5)
                if st.form_submit_button("Create Product", type="primary"):
                    try:
                        with SessionLocal() as session:
                            InventoryService(session).create_product(
                                ProductCreate(
                                    sku=sku,
                                    name=name,
                                    category=category,
                                    unit_cost=unit_cost,
                                    reorder_point=int(rop),
                                    reorder_quantity=int(roq),
                                    supplier=supplier,
                                )
                            )
                        invalidate_caches()
                        st.success(f"Created product {sku}!")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not create product: {exc}")

    with qa2:
        with st.popover("📦 Adjust Stock", width="stretch"):
            with SessionLocal() as session:
                svc = InventoryService(session)
                prods = svc.list_products(page_size=200).items
                whs = svc.list_warehouses()
            with st.form("quick_adjust_stock_form", clear_on_submit=True):
                prod_sku = st.selectbox(
                    "Product",
                    options=[p.sku for p in prods],
                    format_func=lambda s: f"{s} - {next((p.name for p in prods if p.sku == s), '')}",
                )
                wh_code = st.selectbox(
                    "Warehouse",
                    options=[w.code for w in whs],
                    format_func=lambda c: f"{c} ({next((w.name for w in whs if w.code == c), '')})",
                )
                change_val = st.number_input(
                    "Quantity Delta (+ inbound, - outbound)",
                    value=25,
                    step=5,
                )
                reason_val = st.selectbox(
                    "Reason",
                    options=[r.value for r in MovementReason],
                )
                if st.form_submit_button("Apply Stock Movement", type="primary"):
                    try:
                        with SessionLocal() as session:
                            InventoryService(session).record_stock_movement(
                                StockMovementCreate(
                                    sku=prod_sku,
                                    warehouse_code=wh_code,
                                    change=int(change_val),
                                    reason=reason_val,
                                    user="dashboard_operator",
                                )
                            )
                        invalidate_caches()
                        st.success(f"Updated {prod_sku} @ {wh_code} by {change_val:+d}")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Movement failed: {exc}")

    with qa3:
        st.download_button(
            label="📥 Export Replenishment CSV",
            data=repl_csv,
            file_name=f"replenishment_{utc_now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            width="stretch",
        )

    with qa4:
        if st.button("✅ Acknowledge All Alerts", width="stretch"):
            with SessionLocal() as session:
                ack_count = InventoryService(session).acknowledge_all_open_alerts(
                    acknowledged_by="dashboard_quick_action"
                )
            invalidate_caches()
            st.toast(f"Acknowledged {ack_count} open alerts.")
            st.rerun()

    st.divider()

    # 3. Charts Row (Warehouse Donut & Category Horizontal Bars)
    ch1, ch2 = st.columns(2)
    with ch1:
        st.subheader("Stock by Warehouse")
        wh_data = summary.get("by_warehouse", [])
        if not wh_data:
            render_empty_state("No Warehouse Stock Data", "Import or seed inventory to view chart.")
        else:
            wh_df = pd.DataFrame(wh_data)
            fig_donut = px.pie(
                wh_df,
                names="warehouse_code",
                values="total_available",
                hole=0.58,
                color_discrete_sequence=[COLOR_PRIMARY, COLOR_HEALTHY, COLOR_LOW],
                hover_data=["warehouse_name", "total_value"],
            )
            fig_donut.update_traces(textposition="inside", textinfo="percent+label")
            fig_donut.update_layout(
                margin=dict(t=15, b=15, l=15, r=15),
                height=320,
                legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5),
            )
            st.plotly_chart(fig_donut, width="stretch")

    with ch2:
        st.subheader("Stock by Category")
        cat_data = summary.get("by_category", [])
        if not cat_data:
            render_empty_state("No Category Data", "Add products to view category distribution.")
        else:
            cat_df = pd.DataFrame(cat_data).sort_values("total_available", ascending=True)
            fig_bar = px.bar(
                cat_df,
                x="total_available",
                y="category",
                orientation="h",
                text="total_available",
                color="total_value",
                color_continuous_scale=["#60a5fa", "#1d4ed8"],
                labels={
                    "total_available": "Available Units",
                    "category": "Category",
                    "total_value": "Value ($)",
                },
            )
            fig_bar.update_traces(textposition="auto")
            fig_bar.update_layout(
                margin=dict(t=15, b=15, l=15, r=15),
                height=320,
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_bar, width="stretch")

    # 4. Stock Trend Over Time with Date Range Selector
    st.subheader("Stock Trend Over Time")
    tr_col1, tr_col2 = st.columns([2, 3])
    with tr_col1:
        preset = st.radio(
            "Window",
            options=["Last 14 Days", "Last 30 Days", "Last 60 Days", "Last 90 Days", "Custom"],
            index=1,
            horizontal=True,
            label_visibility="collapsed",
        )
    now = utc_now()
    if preset == "Custom":
        with tr_col2:
            date_range = st.date_input(
                "Select Date Range",
                value=((now - timedelta(days=30)).date(), now.date()),
            )
            if isinstance(date_range, tuple) and len(date_range) == 2:
                start_dt = datetime.combine(date_range[0], datetime.min.time())
                end_dt = datetime.combine(date_range[1], datetime.max.time())
            else:
                start_dt = now - timedelta(days=30)
                end_dt = now
    else:
        days_map = {
            "Last 14 Days": 14,
            "Last 30 Days": 30,
            "Last 60 Days": 60,
            "Last 90 Days": 90,
        }
        start_dt = now - timedelta(days=days_map[preset])
        end_dt = now

    trend_rows = cached_stock_trend(start_dt.isoformat(), end_dt.isoformat())
    if not trend_rows:
        render_empty_state("No Movement History", "Record stock movements to view historical trend.")
    else:
        trend_df = pd.DataFrame(trend_rows)
        fig_line = go.Figure()
        fig_line.add_trace(
            go.Scatter(
                x=trend_df["date"],
                y=trend_df["total_stock_units"],
                mode="lines+markers",
                name="Total Stock Units",
                line=dict(color=COLOR_PRIMARY, width=3),
                fill="tozeroy",
                fillcolor="rgba(37, 99, 235, 0.08)",
            )
        )
        fig_line.update_layout(
            margin=dict(t=15, b=15, l=15, r=15),
            height=290,
            xaxis_title="Date",
            yaxis_title="Total Units On Hand",
            hovermode="x unified",
        )
        st.plotly_chart(fig_line, width="stretch")

    st.divider()

    # 5. Low Stock Alerts Panel & Recent Activity Feed
    col_alerts, col_activity = st.columns([3, 2])

    with col_alerts:
        hdr_a, hdr_b = st.columns([4, 1])
        with hdr_a:
            st.subheader("Low Stock & Critical Stock Alerts")
        with hdr_b:
            if st.button("View all →", key="view_all_alerts_btn"):
                st.session_state["nav_page"] = "Alerts"
                st.rerun()

        with SessionLocal() as session:
            svc = InventoryService(session)
            stock_items = svc.list_stock_levels(page_size=300).items

        low_or_out = [
            s
            for s in stock_items
            if s.status
            in {StockStatus.OUT_OF_STOCK, StockStatus.LOW_STOCK, StockStatus.ANOMALY}
        ]
        low_or_out.sort(key=lambda x: (x.available, x.sku))

        if not low_or_out:
            render_empty_state(
                "All Products Healthy",
                "Zero products are currently below their reorder point.",
            )
        else:
            alert_table_rows = []
            for item in low_or_out[:10]:
                sev_badge = (
                    "🔴 CRITICAL"
                    if item.available <= 0 or item.status == StockStatus.ANOMALY
                    else "🟠 WARNING"
                )
                alert_table_rows.append(
                    {
                        "Product": f"{item.sku} — {item.product_name}",
                        "Category": item.category,
                        "Warehouse": item.warehouse_code,
                        "Available": item.available,
                        "Reorder Point": item.reorder_point,
                        "Severity": sev_badge,
                    }
                )
            st.dataframe(
                pd.DataFrame(alert_table_rows),
                hide_index=True,
                width="stretch",
            )

    with col_activity:
        st.subheader("Recent Activity Feed")
        with SessionLocal() as session:
            svc = InventoryService(session)
            recent_movs = svc.list_stock_movements(page=1, page_size=6).items
            recent_alerts = svc.list_alerts(page=1, page_size=6).items

        events: list[tuple[datetime, str, str, str]] = []
        for m in recent_movs:
            sign = f"+{m.change}" if m.change > 0 else str(m.change)
            color = COLOR_HEALTHY if m.change > 0 else COLOR_LOW
            events.append(
                (
                    m.timestamp,
                    color,
                    f"<strong>Movement ({m.reason})</strong>: {m.sku} @ {m.warehouse_code} ({sign} units by {m.user})",
                    m.timestamp.strftime("%Y-%m-%d %H:%M"),
                )
            )
        for a in recent_alerts:
            color = (
                COLOR_CRITICAL
                if str(a.severity).lower() == "critical"
                else COLOR_LOW
            )
            events.append(
                (
                    a.created_at,
                    color,
                    f"<strong>Alert [{str(a.type)}]</strong>: {a.sku} @ {a.warehouse_code}",
                    a.created_at.strftime("%Y-%m-%d %H:%M"),
                )
            )

        events.sort(key=lambda x: x[0], reverse=True)
        if not events:
            render_empty_state("No Recent Activity", "Movements and alerts will appear here.")
        else:
            for _, border_color, html_desc, ts_str in events[:8]:
                st.markdown(
                    f"""
                    <div class="activity-item" style="border-left-color: {border_color};">
                        <div>{html_desc}</div>
                        <div style="font-size:0.75rem; color:#64748b; margin-top:2px;">{ts_str} UTC</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )


def render_inventory_page() -> None:
    """Render the searchable & filterable Inventory table and CSV Import studio."""
    st.title("Inventory Explorer & CSV Import")
    render_system_status_bar()

    tab_explore, tab_import = st.tabs(["📋 Search & Filter Inventory", "📤 Import CSV Data"])

    with tab_explore:
        with SessionLocal() as session:
            svc = InventoryService(session)
            warehouses = svc.list_warehouses()

        f1, f2, f3, f4 = st.columns([2, 1.5, 1.5, 1.5])
        with f1:
            search_q = st.text_input("Search SKU, Product Name, or Supplier", "")
        with f2:
            wh_filter = st.selectbox(
                "Warehouse",
                options=["All"] + [w.code for w in warehouses],
            )
        with f3:
            cat_filter = st.selectbox(
                "Category",
                options=[
                    "All",
                    "Electronics",
                    "Industrial Hardware",
                    "Office Supplies",
                    "Packaging & Logistics",
                ],
            )
        with f4:
            status_filter = st.selectbox(
                "Stock Status",
                options=[
                    "All",
                    StockStatus.HEALTHY.value,
                    StockStatus.LOW_STOCK.value,
                    StockStatus.OUT_OF_STOCK.value,
                    StockStatus.OVERSTOCK.value,
                    StockStatus.ANOMALY.value,
                ],
            )

        with SessionLocal() as session:
            svc = InventoryService(session)
            stock_page = svc.list_stock_levels(
                warehouse=None if wh_filter == "All" else wh_filter,
                category=None if cat_filter == "All" else cat_filter,
                status=None if status_filter == "All" else status_filter,
                search=search_q if search_q.strip() else None,
                page=1,
                page_size=500,
            )

        if not stock_page.items:
            render_empty_state(
                "No Matching Inventory Records",
                "Try adjusting your search query, warehouse, category, or status filters.",
            )
        else:
            status_badge_map = {
                StockStatus.HEALTHY: "🟢 HEALTHY",
                StockStatus.LOW_STOCK: "🟠 LOW STOCK",
                StockStatus.OUT_OF_STOCK: "🔴 OUT OF STOCK",
                StockStatus.OVERSTOCK: "🟠 OVERSTOCK",
                StockStatus.ANOMALY: "🔴 ANOMALY",
            }
            rows = [
                {
                    "SKU": item.sku,
                    "Product": item.product_name,
                    "Category": item.category,
                    "Supplier": item.supplier,
                    "Warehouse": item.warehouse_code,
                    "On Hand": item.quantity_on_hand,
                    "Reserved": item.quantity_reserved,
                    "Available": item.available,
                    "Reorder Pt": item.reorder_point,
                    "Days Left": (
                        "∞"
                        if item.days_of_stock_remaining is None
                        else f"{item.days_of_stock_remaining:.1f}d"
                    ),
                    "Unit Cost ($)": f"${item.unit_cost:,.2f}",
                    "Stock Value ($)": f"${item.stock_value:,.2f}",
                    "Status": status_badge_map.get(item.status, item.status.value),
                }
                for item in stock_page.items
            ]
            st.caption(f"Showing {len(rows)} inventory records.")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=520)

    with tab_import:
        st.subheader("Bulk CSV Import with Row-Level Validation")
        st.markdown(
            """
            - **Products CSV columns**: `sku,name,category,unit_cost,reorder_point,reorder_quantity,supplier`
            - **Stock Levels CSV columns**: `sku,warehouse_code,quantity_on_hand,quantity_reserved`
            """
        )
        imp_type = st.radio(
            "Select Import Type",
            options=["Products Catalog", "Warehouse Stock Levels"],
            horizontal=True,
        )
        uploaded = st.file_uploader("Upload CSV File", type=["csv"])
        if uploaded is not None and st.button("Validate & Import CSV", type="primary"):
            raw_bytes = uploaded.getvalue()
            with SessionLocal() as session:
                if imp_type == "Products Catalog":
                    report = import_products_csv(session, raw_bytes)
                else:
                    report = import_stock_levels_csv(
                        session, raw_bytes, user="dashboard_importer"
                    )
                    MonitoringEngine(session).run_check()
            invalidate_caches()

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Rows", report.total_rows)
            m2.metric("Created", report.imported_count)
            m3.metric("Updated", report.updated_count)
            m4.metric("Rejected Rows", report.failed_count)

            if report.errors:
                st.warning(f"{report.failed_count} row(s) failed validation:")
                err_df = pd.DataFrame(
                    [
                        {
                            "Row #": e.row_number,
                            "Field": e.field,
                            "Error Message": e.error_message,
                            "Raw Data": str(e.raw_data),
                        }
                        for e in report.errors
                    ]
                )
                st.dataframe(err_df, hide_index=True, width="stretch")
            else:
                st.success("All rows imported cleanly with zero validation errors!")


def render_alerts_page() -> None:
    """Render the Alert History & Management page with per-alert and bulk acknowledgment."""
    st.title("Alerts & Anomaly Detection")
    render_system_status_bar()

    top1, top2, top3, top4, top5 = st.columns([1.4, 1.4, 1.6, 1.2, 1.4])
    with top1:
        state_filter = st.selectbox(
            "Alert State",
            options=["Open (Unacknowledged)", "Resolved / Acknowledged", "All Alerts"],
        )
    with top2:
        sev_filter = st.selectbox(
            "Severity",
            options=["All", AlertSeverity.CRITICAL.value, AlertSeverity.WARNING.value],
        )
    with top3:
        type_filter = st.selectbox(
            "Alert Type",
            options=["All"] + [t.value for t in AlertType],
        )
    with top4:
        st.write("")
        if st.button("🔄 Run Check Now", width="stretch"):
            with SessionLocal() as session:
                res = MonitoringEngine(session).run_check()
            invalidate_caches()
            st.toast(
                f"Evaluated {res.evaluated_count} items ({res.new_alerts_created} new alerts)."
            )
            st.rerun()
    with top5:
        st.write("")
        if st.button("✅ Acknowledge All Open", type="primary", width="stretch"):
            with SessionLocal() as session:
                cnt = InventoryService(session).acknowledge_all_open_alerts("dashboard_user")
            invalidate_caches()
            st.toast(f"Acknowledged {cnt} alerts.")
            st.rerun()

    active_bool: bool | None = None
    if state_filter == "Open (Unacknowledged)":
        active_bool = True
    elif state_filter == "Resolved / Acknowledged":
        active_bool = False

    with SessionLocal() as session:
        alerts_page = InventoryService(session).list_alerts(
            severity=None if sev_filter == "All" else sev_filter,
            alert_type=None if type_filter == "All" else type_filter,
            active_only=active_bool,
            page=1,
            page_size=200,
        )

    if not alerts_page.items:
        render_empty_state(
            "No Alerts Match Current Filters",
            "Inventory levels are within configured thresholds for this filter selection.",
        )
        return

    st.caption(f"Displaying {len(alerts_page.items)} alert(s).")
    for alert in alerts_page.items:
        is_crit = str(alert.severity).lower() == "critical"
        badge_cls = "badge-critical" if is_crit else "badge-low"
        border_col = COLOR_CRITICAL if is_crit else COLOR_LOW

        row_c1, row_c2 = st.columns([5, 1])
        with row_c1:
            status_tag = (
                '<span class="badge badge-healthy">RESOLVED / ACK</span>'
                if not alert.is_active or alert.acknowledged_at
                else f'<span class="badge {badge_cls}">{str(alert.severity).upper()}</span>'
            )
            st.markdown(
                f"""
                <div class="activity-item" style="border-left-color: {border_col};">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            {status_tag}
                            <strong style="margin-left:8px;">{alert.type}</strong> —
                            <code>{alert.sku}</code> ({alert.product_name}) @ <strong>{alert.warehouse_code}</strong>
                        </div>
                        <span style="font-size:0.78rem; color:#64748b;">
                            {alert.created_at.strftime('%Y-%m-%d %H:%M')} UTC
                        </span>
                    </div>
                    <div style="margin-top:6px; color:#334155;">{alert.message}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with row_c2:
            if alert.is_active and alert.acknowledged_at is None:
                if st.button("Acknowledge", key=f"ack_btn_{alert.id}", width="stretch"):
                    with SessionLocal() as session:
                        InventoryService(session).acknowledge_alert(
                            alert.id, acknowledged_by="dashboard_user"
                        )
                    invalidate_caches()
                    st.rerun()
            else:
                ack_ts = (
                    alert.acknowledged_at.strftime("%m-%d %H:%M")
                    if alert.acknowledged_at
                    else "Auto"
                )
                st.caption(f"Closed ({ack_ts})")


def render_replenishment_page() -> None:
    """Render Replenishment Recommendations grouped by supplier with CSV export."""
    st.title("Smart Replenishment & Purchase Planning")
    render_system_status_bar()

    report_dict, csv_text = cached_replenishment_data()

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        render_kpi_card(
            "Suppliers to Order From",
            str(report_dict["total_suppliers"]),
            "Grouped purchase orders",
            COLOR_PRIMARY,
        )
    with k2:
        render_kpi_card(
            "Replenishment Line Items",
            str(report_dict["total_line_items"]),
            "Low stock & stockout risk",
            COLOR_LOW,
        )
    with k3:
        render_kpi_card(
            "Total Units to Order",
            f"{report_dict['total_units_to_order']:,}",
            "Recommended order volume",
            COLOR_HEALTHY,
        )
    with k4:
        render_kpi_card(
            "Total Estimated PO Cost",
            f"${report_dict['total_estimated_cost']:,.2f}",
            "Based on catalog unit costs",
            COLOR_PRIMARY,
        )

    st.write("")
    st.download_button(
        label="📥 Download Full Replenishment Schedule (CSV)",
        data=csv_text,
        file_name=f"replenishment_by_supplier_{utc_now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        type="primary",
    )

    supplier_groups = report_dict.get("supplier_groups", [])
    if not supplier_groups:
        render_empty_state(
            "No Replenishment Needed",
            "All products across all warehouses have sufficient stock and healthy forecast buffers.",
        )
        return

    for group in supplier_groups:
        header = (
            f"🏢 {group['supplier']} — {group['total_items']} item(s) | "
            f"{group['total_units_to_order']:,} units | "
            f"Est. Total: ${group['total_estimated_cost']:,.2f}"
        )
        with st.expander(header, expanded=True):
            df_items = pd.DataFrame(
                [
                    {
                        "Urgency": (
                            "🔴 CRITICAL"
                            if i["urgency"] == "CRITICAL"
                            else ("🟠 HIGH" if i["urgency"] == "HIGH" else "🟡 MEDIUM")
                        ),
                        "SKU": i["sku"],
                        "Product": i["product_name"],
                        "Category": i["category"],
                        "Warehouse": i["warehouse_code"],
                        "Available": i["available"],
                        "Reorder Pt": i["reorder_point"],
                        "Avg Daily Use": f"{i['avg_daily_consumption']:.2f}/d",
                        "Days Remaining": (
                            "∞"
                            if i["days_of_stock_remaining"] is None
                            else f"{i['days_of_stock_remaining']:.1f}d"
                        ),
                        "Suggested Order Qty": i["suggested_order_qty"],
                        "Unit Cost": f"${i['unit_cost']:,.2f}",
                        "Estimated Line Cost": f"${i['estimated_cost']:,.2f}",
                    }
                    for i in group["items"]
                ]
            )
            st.dataframe(df_items, hide_index=True, width="stretch")


def render_movements_page() -> None:
    """Render the Stock Movements ledger, analytics chart, and movement entry form."""
    st.title("Stock Movements Ledger")
    render_system_status_bar()

    with SessionLocal() as session:
        svc = InventoryService(session)
        products = svc.list_products(page_size=250).items
        warehouses = svc.list_warehouses()

    with st.expander("➕ Record New Stock Movement", expanded=False):
        with st.form("movement_page_form", clear_on_submit=True):
            mc1, mc2, mc3, mc4 = st.columns(4)
            with mc1:
                sel_sku = st.selectbox(
                    "Product SKU",
                    options=[p.sku for p in products],
                    format_func=lambda s: f"{s} — {next((p.name for p in products if p.sku == s), '')}",
                )
            with mc2:
                sel_wh = st.selectbox(
                    "Warehouse",
                    options=[w.code for w in warehouses],
                )
            with mc3:
                delta_qty = st.number_input("Quantity Change (+/-)", value=10, step=1)
            with mc4:
                sel_reason = st.selectbox("Reason", [r.value for r in MovementReason])
            operator_name = st.text_input("Recorded By", value="warehouse_operator")

            if st.form_submit_button("Submit Movement", type="primary"):
                try:
                    with SessionLocal() as session:
                        res = InventoryService(session).record_stock_movement(
                            StockMovementCreate(
                                sku=sel_sku,
                                warehouse_code=sel_wh,
                                change=int(delta_qty),
                                reason=sel_reason,
                                user=operator_name,
                            )
                        )
                    invalidate_caches()
                    st.success(
                        f"Movement #{res.id} recorded! New available balance for "
                        f"{res.sku} @ {res.warehouse_code}: {res.new_available} units."
                    )
                    st.rerun()
                except InventoryError as exc:
                    st.error(exc.message)
                except Exception as exc:
                    st.error(f"Error recording movement: {exc}")

    f1, f2, f3 = st.columns(3)
    with f1:
        wh_id_filter = st.selectbox(
            "Filter by Warehouse",
            options=[None] + [w.id for w in warehouses],
            format_func=lambda wid: "All Warehouses"
            if wid is None
            else next((f"{w.code} - {w.name}" for w in warehouses if w.id == wid), str(wid)),
        )
    with f2:
        reason_filter = st.selectbox(
            "Filter by Reason",
            options=["All"] + [r.value for r in MovementReason],
        )
    with f3:
        days_filter = st.selectbox(
            "Time Horizon",
            options=[7, 30, 60, 90],
            index=1,
            format_func=lambda d: f"Last {d} Days",
        )

    with SessionLocal() as session:
        mov_page = InventoryService(session).list_stock_movements(
            warehouse_id=wh_id_filter,
            reason=None if reason_filter == "All" else reason_filter,
            days=days_filter,
            page=1,
            page_size=250,
        )

    if not mov_page.items:
        render_empty_state("No Stock Movements Found", "No movements match the selected filters.")
        return

    mov_df = pd.DataFrame(
        [
            {
                "ID": m.id,
                "Timestamp (UTC)": m.timestamp.strftime("%Y-%m-%d %H:%M"),
                "SKU": m.sku,
                "Product": m.product_name,
                "Warehouse": m.warehouse_code,
                "Change": m.change,
                "Direction": "Inbound (+)" if m.change > 0 else "Outbound (-)",
                "Reason": m.reason,
                "User": m.user,
            }
            for m in mov_page.items
        ]
    )
    st.dataframe(mov_df, hide_index=True, width="stretch", height=480)


def render_settings_page() -> None:
    """Render Threshold Settings, Notification Channels, and Folder-Watch Sync controls."""
    st.title("System Settings & Notification Channels")
    render_system_status_bar()

    settings = get_settings()
    tab_thresh, tab_notif, tab_sync = st.tabs(
        ["🎯 Alert Thresholds", "🔔 Notification Channels", "🔄 External Source Sync"]
    )

    with tab_thresh:
        with SessionLocal() as session:
            svc = InventoryService(session)
            current_settings = svc.get_threshold_settings()
            products = svc.list_products(page_size=250).items

        st.subheader("Configured Threshold Rules")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Scope": "GLOBAL DEFAULT" if s.product_id is None else f"{s.sku} ({s.product_name})",
                        "Low Stock Override": (
                            "Uses Product Reorder Point"
                            if s.low_stock_threshold is None
                            else str(s.low_stock_threshold)
                        ),
                        "Overstock Max": s.overstock_threshold,
                        "Slow-Moving (Days)": s.slow_moving_days,
                        "Forecast Lookback (Days)": s.forecast_lookback_days,
                        "Forecast Warning (Days)": s.forecast_warning_days,
                        "Updated At": s.updated_at.strftime("%Y-%m-%d %H:%M"),
                    }
                    for s in current_settings
                ]
            ),
            hide_index=True,
            width="stretch",
        )

        st.subheader("Update Global or Per-Product Thresholds")
        global_cfg = next((s for s in current_settings if s.product_id is None), current_settings[0])
        with st.form("threshold_update_form"):
            scope_choice = st.selectbox(
                "Target Scope",
                options=[None] + [p.id for p in products],
                format_func=lambda pid: "Global Default (All Products)"
                if pid is None
                else next((f"{p.sku} — {p.name}" for p in products if p.id == pid), str(pid)),
            )
            c1, c2, c3 = st.columns(3)
            with c1:
                low_override = st.number_input(
                    "Low Stock Threshold Override (0 = use product reorder point)",
                    min_value=0,
                    value=int(global_cfg.low_stock_threshold or 0),
                )
                overstock_val = st.number_input(
                    "Overstock Maximum Units",
                    min_value=10,
                    value=int(global_cfg.overstock_threshold),
                )
            with c2:
                slow_days_val = st.number_input(
                    "Slow-Moving Inactivity Days",
                    min_value=1,
                    value=int(global_cfg.slow_moving_days),
                )
                lookback_val = st.number_input(
                    "Forecast Lookback Window (Days)",
                    min_value=7,
                    value=int(global_cfg.forecast_lookback_days),
                )
            with c3:
                warn_days_val = st.number_input(
                    "Stockout Forecast Warning (Days)",
                    min_value=1,
                    value=int(global_cfg.forecast_warning_days),
                )

            if st.form_submit_button("Save Threshold Settings", type="primary"):
                with SessionLocal() as session:
                    svc = InventoryService(session)
                    svc.upsert_threshold_setting(
                        ThresholdSettingUpdate(
                            product_id=scope_choice,
                            low_stock_threshold=int(low_override) if low_override > 0 else None,
                            overstock_threshold=int(overstock_val),
                            slow_moving_days=int(slow_days_val),
                            forecast_lookback_days=int(lookback_val),
                            forecast_warning_days=int(warn_days_val),
                        )
                    )
                    MonitoringEngine(session).run_check()
                invalidate_caches()
                st.success("Threshold settings saved and monitoring re-evaluated!")
                st.rerun()

    with tab_notif:
        st.subheader("Notification Channels & Resilience Policy")
        nc1, nc2, nc3, nc4 = st.columns(4)
        nc1.metric("Console Logger", "Enabled" if settings.enable_console_notifications else "Disabled")
        nc2.metric("Email (SMTP)", "Enabled" if settings.enable_email_notifications else "Disabled")
        nc3.metric("Telegram Bot", "Enabled" if settings.enable_telegram_notifications else "Disabled")
        nc4.metric("Slack Webhook", "Enabled" if settings.enable_slack_notifications else "Disabled")

        st.markdown(
            f"""
            - **Throttle Cooldown**: `{settings.notification_throttle_minutes}` minutes per unique alert key
            - **Quiet Hours**: `{settings.quiet_hours_start}` to `{settings.quiet_hours_end}`
              (Enabled: `{settings.quiet_hours_enabled}`, Critical Bypass: `{settings.quiet_hours_allow_critical}`)
            - **Retry Policy**: Up to `{settings.max_notification_retries}` attempts with exponential backoff
            """
        )

        if st.button("📨 Send Test Notification Now"):
            with SessionLocal() as session:
                dispatcher = NotificationDispatcher(session)
                outcomes = dispatcher.send_payload(
                    NotificationPayload(
                        alert_key=f"test_ping:{int(time.time())}",
                        subject="Inventory Monitor Test Notification",
                        text_body="This is a verification ping from the Streamlit Settings page.",
                        severity=AlertSeverity.CRITICAL,
                    )
                )
            st.success(f"Dispatched test notification: {outcomes}")

        st.subheader("Recent Notification Audit Log")
        with SessionLocal() as session:
            logs = list(
                session.scalars(
                    select(NotificationLog)
                    .order_by(desc(NotificationLog.created_at), desc(NotificationLog.id))
                    .limit(30)
                ).all()
            )
        if not logs:
            render_empty_state("No Notification Logs Yet", "Trigger a check or test notification.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Time (UTC)": lg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                            "Channel": lg.channel,
                            "Alert Key": lg.alert_key,
                            "Subject": lg.subject,
                            "Status": lg.status,
                            "Attempts": lg.attempts,
                            "Error": lg.error_message or "",
                        }
                        for lg in logs
                    ]
                ),
                hide_index=True,
                width="stretch",
            )

    with tab_sync:
        st.subheader("CSV Folder-Watch & External ERP Adapter")
        st.markdown(
            f"""
            Drop `products_*.csv` or `stock_*.csv` files into `{settings.watch_folder_path}`
            for automatic ingestion and archiving.
            """
        )
        if st.button("🔄 Trigger Folder-Watch Sync Now", type="primary"):
            with SessionLocal() as session:
                sync_res = CSVFolderWatchSource().sync(session)
                MonitoringEngine(session).run_check()
            invalidate_caches()
            st.success(
                f"Sync complete! Files processed: {sync_res.files_processed}, "
                f"Products (+{sync_res.products_created}/~{sync_res.products_updated}), "
                f"Stock (+{sync_res.stock_levels_created}/~{sync_res.stock_levels_updated})."
            )


def main() -> None:
    """Main Streamlit application entrypoint."""
    st.set_page_config(
        page_title="Automate Inventory Monitoring",
        page_icon="📦",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_custom_css()
    ensure_seeded_and_checked()

    if "nav_page" not in st.session_state:
        st.session_state["nav_page"] = "Dashboard"

    with st.sidebar:
        st.markdown("## 📦 Inventory Monitor")
        st.caption("Automated Stock & Alert Intelligence")
        st.divider()

        selected_page = st.radio(
            "Navigation",
            options=NAVIGATION_PAGES,
            index=NAVIGATION_PAGES.index(st.session_state["nav_page"]),
        )
        if selected_page != st.session_state["nav_page"]:
            st.session_state["nav_page"] = selected_page
            st.rerun()

        st.divider()
        st.markdown("### ⚡ Live Controls")
        auto_refresh = st.toggle("Enable Auto-Refresh", value=False)
        refresh_sec = st.selectbox(
            "Refresh Interval",
            options=[15, 30, 60],
            index=1,
            format_func=lambda s: f"Every {s} seconds",
            disabled=not auto_refresh,
        )
        if st.button("🔄 Refresh Data", width="stretch"):
            invalidate_caches()
            st.rerun()

    page = st.session_state["nav_page"]
    if page == "Dashboard":
        render_dashboard_page()
    elif page == "Inventory":
        render_inventory_page()
    elif page == "Alerts":
        render_alerts_page()
    elif page == "Replenishment":
        render_replenishment_page()
    elif page == "Movements":
        render_movements_page()
    elif page == "Settings":
        render_settings_page()

    if auto_refresh:
        time.sleep(int(refresh_sec))
        invalidate_caches()
        st.rerun()


if __name__ == "__main__":
    main()
