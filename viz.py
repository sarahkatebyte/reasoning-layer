#!/usr/bin/env python3
"""
Astrid Spend Visualizer
Run: streamlit run viz.py
"""

import sqlite3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from datetime import datetime

DB_PATH = "spend.db"

st.set_page_config(
    page_title="Astrid Spend",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    .metric-label { font-size: 12px !important; }
    h1 { font-size: 1.6rem !important; }
    h2 { font-size: 1.1rem !important; color: #a78bfa; }
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=30)
def load_data():
    conn = sqlite3.connect(DB_PATH)

    snapshots = pd.read_sql("""
        SELECT
            ts,
            datetime(ts/1000, 'unixepoch', 'localtime') as dt_str,
            call_site,
            cost_usd,
            input_tok,
            output_tok,
            cache_read,
            events
        FROM snapshots
        ORDER BY ts ASC
    """, conn)
    snapshots["dt"] = pd.to_datetime(snapshots["dt_str"])

    events = pd.read_sql("""
        SELECT ts, datetime(ts/1000, 'unixepoch', 'localtime') as dt_str, note
        FROM opt_events ORDER BY ts ASC
    """, conn)
    if not events.empty:
        events["dt"] = pd.to_datetime(events["dt_str"])

    conn.close()
    return snapshots, events


def totals_over_time(snapshots):
    # Each poll is a 7-day rolling window — plot the window value at each point in time
    return (
        snapshots.groupby("ts")["cost_usd"]
        .sum()
        .reset_index()
        .rename(columns={"cost_usd": "total_cost"})
        .assign(dt=lambda df: pd.to_datetime(df["ts"], unit="ms"))
    )


# ── Load ─────────────────────────────────────────────────────────
snapshots, events = load_data()

# ── Header ───────────────────────────────────────────────────────
st.title("✦ Astrid — LLM Spend Tracker")
st.caption(f"Data: {snapshots['dt'].min().strftime('%b %d')} → {snapshots['dt'].max().strftime('%b %d, %Y')}  ·  {snapshots['ts'].nunique()} polls  ·  metrics = 7-day rolling window  ·  auto-refreshes every 30s")

# ── Top metrics — use latest snapshot only (each row is a 7-day rolling window) ──
latest_ts    = snapshots["ts"].max()
latest       = snapshots[snapshots["ts"] == latest_ts]
total_cost   = latest["cost_usd"].sum()
total_input  = latest["input_tok"].sum()
total_output = latest["output_tok"].sum()
total_cache  = latest["cache_read"].sum()
top_site     = latest.set_index("call_site")["cost_usd"].idxmax()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Spend", f"${total_cost:,.2f}")
c2.metric("Input Tokens", f"{total_input/1_000_000:.1f}M")
c3.metric("Output Tokens", f"{total_output/1_000_000:.1f}M")
c4.metric("Cache Reads", f"{total_cache/1_000_000:.1f}M")
c5.metric("Top Cost Driver", top_site)

st.divider()

# ── Spend over time ──────────────────────────────────────────────
st.subheader("7-day rolling spend — the delta is the signal")

totals = totals_over_time(snapshots)
totals["cumulative"] = totals["total_cost"].cumsum()

fig_time = go.Figure()
fig_time.add_trace(go.Scatter(
    x=totals["dt"], y=totals["total_cost"],
    mode="lines", line=dict(color="#a78bfa", width=2),
    name="7-day rolling spend",
    hovertemplate="%{x|%b %d %H:%M}<br>$%{y:,.4f}<extra></extra>"
))

# Overlay optimization events
if not events.empty:
    for _, ev in events.iterrows():
        ev_dt = ev["dt"]
        closest_idx = (totals["dt"] - ev_dt).abs().idxmin()
        y_val = totals.loc[closest_idx, "cumulative"]
        fig_time.add_vline(
            x=ev_dt, line_dash="dash", line_color="#34d399", line_width=1.5,
            annotation_text=ev["note"], annotation_position="top left",
            annotation_font_color="#34d399"
        )

fig_time.update_layout(
    height=300, margin=dict(t=20, b=20),
    plot_bgcolor="#0a0a0f", paper_bgcolor="#0a0a0f",
    font_color="#e2e8f0",
    xaxis=dict(gridcolor="#1e1e2e", showgrid=True),
    yaxis=dict(gridcolor="#1e1e2e", showgrid=True, tickprefix="$"),
    showlegend=False,
)
st.plotly_chart(fig_time, use_container_width=True)

# ── Call site breakdown ──────────────────────────────────────────
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("Spend by call site (7-day window)")
    by_site = (
        latest.groupby("call_site")["cost_usd"]
        .sum()
        .sort_values(ascending=True)
        .reset_index()
    )
    fig_bar = px.bar(
        by_site, x="cost_usd", y="call_site", orientation="h",
        color="cost_usd", color_continuous_scale="Purples",
        labels={"cost_usd": "Total cost (USD)", "call_site": ""},
    )
    fig_bar.update_layout(
        height=400, margin=dict(t=10, b=10),
        plot_bgcolor="#0a0a0f", paper_bgcolor="#0a0a0f",
        font_color="#e2e8f0", coloraxis_showscale=False,
        xaxis=dict(gridcolor="#1e1e2e", tickprefix="$"),
        yaxis=dict(gridcolor="#1e1e2e"),
    )
    st.plotly_chart(fig_bar, use_container_width=True)

with col_right:
    st.subheader("Cost share")
    by_site_pie = snapshots.groupby("call_site")["cost_usd"].sum().reset_index()
    fig_pie = px.pie(
        by_site_pie, values="cost_usd", names="call_site",
        color_discrete_sequence=px.colors.sequential.Purples_r,
        hole=0.45,
    )
    fig_pie.update_layout(
        height=400, margin=dict(t=10, b=10),
        paper_bgcolor="#0a0a0f", font_color="#e2e8f0",
        legend=dict(font=dict(size=11)),
    )
    fig_pie.update_traces(textposition="inside", textinfo="percent")
    st.plotly_chart(fig_pie, use_container_width=True)

# ── Per call site over time ──────────────────────────────────────
st.subheader("Call site spend — over time")

top_sites = (
    snapshots.groupby("call_site")["cost_usd"]
    .sum().nlargest(8).index.tolist()
)
selected = st.multiselect("Select call sites", top_sites, default=top_sites[:5])

if selected:
    filtered = snapshots[snapshots["call_site"].isin(selected)]
    pivot = (
        filtered.groupby(["dt", "call_site"])["cost_usd"]
        .sum().reset_index()
    )
    fig_lines = px.line(
        pivot, x="dt", y="cost_usd", color="call_site",
        labels={"cost_usd": "Cost (USD)", "dt": "", "call_site": ""},
        color_discrete_sequence=px.colors.qualitative.Pastel,
    )
    fig_lines.update_layout(
        height=320, margin=dict(t=10, b=10),
        plot_bgcolor="#0a0a0f", paper_bgcolor="#0a0a0f",
        font_color="#e2e8f0",
        xaxis=dict(gridcolor="#1e1e2e"),
        yaxis=dict(gridcolor="#1e1e2e", tickprefix="$"),
    )
    st.plotly_chart(fig_lines, use_container_width=True)

# ── Before / after optimization ───────────────────────────────────
if not events.empty:
    st.divider()
    st.subheader("Before / after optimization")

    event_options = events["note"].tolist()
    chosen = st.selectbox("Optimization event", event_options)
    ev_ts = int(events.loc[events["note"] == chosen, "ts"].iloc[0])

    window_hrs = st.slider("Comparison window (hours each side)", 1, 48, 24)
    window_ms = window_hrs * 3_600_000

    before = snapshots[(snapshots["ts"] >= ev_ts - window_ms) & (snapshots["ts"] < ev_ts)]
    after  = snapshots[(snapshots["ts"] >= ev_ts) & (snapshots["ts"] < ev_ts + window_ms)]

    b_cost = before["cost_usd"].sum()
    a_cost = after["cost_usd"].sum()
    delta  = a_cost - b_cost
    pct    = (delta / b_cost * 100) if b_cost > 0 else 0

    d1, d2, d3 = st.columns(3)
    d1.metric("Before", f"${b_cost:,.4f}")
    d2.metric("After",  f"${a_cost:,.4f}")
    d3.metric("Delta",  f"${delta:,.4f}", delta=f"{pct:.1f}%", delta_color="inverse")

else:
    st.divider()
    st.info("No optimization events logged yet. Run `python tracker.py event 'ran llm-cost-optimizer'` after your first optimization to unlock before/after comparison.")

# ── Raw data ─────────────────────────────────────────────────────
with st.expander("Raw data"):
    st.dataframe(
        snapshots.groupby("call_site").agg(
            total_cost=("cost_usd", "sum"),
            total_calls=("events", "sum"),
            avg_cost_per_call=("cost_usd", "mean"),
        ).sort_values("total_cost", ascending=False).round(6),
        use_container_width=True,
    )
