import os
from contextlib import closing

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
from dash import Dash, Input, Output, dcc, html
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
REFRESH_OPTIONS = [10, 60]  # segundos entre actualizaciones automáticas
REFRESH_SECONDS = 60        # valor inicial
RANGES = {"1h": 1, "3h": 3, "24h": 24}  # etiqueta: horas


def query(sql, params=None):
    with closing(psycopg2.connect(DATABASE_URL, connect_timeout=5)) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [d[0] for d in cur.description]
            df = pd.DataFrame(cur.fetchall(), columns=columns)
    # PostgreSQL devuelve números como Decimal; los pasamos a float para graficar.
    for col in df.columns:
        if col not in ("bucket", "svc"):
            df[col] = pd.to_numeric(df[col])
    return df


def load_services():
    try:
        df = query("SELECT DISTINCT svc FROM logs WHERE svc IS NOT NULL ORDER BY svc")
        return df["svc"].tolist()
    except Exception as exc:
        print(f"No se pudieron cargar los servicios: {exc}")
        return []


def build_filter(service, hours):
    clauses = ["ts >= NOW() - make_interval(hours => %s)"]
    params = [hours]
    if service and service != "ALL":
        clauses.append("svc = %s")
        params.append(service)
    return "WHERE " + " AND ".join(clauses), params


def get_totals(service, hours):
    where, params = build_filter(service, hours)
    sql = f"""
        SELECT COUNT(*) AS requests,
               AVG(duration) AS avg_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration) AS p95_ms,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY duration) AS p99_ms,
               COUNT(*) FILTER (WHERE status_code BETWEEN 400 AND 499) AS err_4xx,
               COUNT(*) FILTER (WHERE status_code BETWEEN 500 AND 599) AS err_5xx
        FROM logs {where}
    """
    return query(sql, params).iloc[0]


def get_series(service, hours):
    unit = "minute" if hours <= 3 else "hour"
    minutes_per_bucket = 1 if unit == "minute" else 60
    where, params = build_filter(service, hours)
    sql = f"""
        SELECT date_trunc(%s, ts AT TIME ZONE 'America/Bogota') AS bucket,
               COUNT(*) AS requests,
               AVG(duration) AS avg_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration) AS p95_ms,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY duration) AS p99_ms,
               COUNT(*) FILTER (WHERE status_code BETWEEN 400 AND 499) AS err_4xx,
               COUNT(*) FILTER (WHERE status_code BETWEEN 500 AND 599) AS err_5xx
        FROM logs {where}
        GROUP BY bucket
        ORDER BY bucket
    """
    df = query(sql, [unit] + params)
    df["rpm"] = pd.to_numeric(df["requests"]) / minutes_per_bucket
    return df


def fmt_ms(value):
    return "—" if pd.isna(value) else f"{value:,.0f} ms"


def card(title, value):
    return html.Div([html.Small(title), html.H3(value)], className="card")


def empty_fig(text="Sin datos en este rango"):
    fig = go.Figure()
    fig.update_layout(title=text, xaxis={"visible": False}, yaxis={"visible": False})
    return fig


services = load_services()

app = Dash(__name__, title="Dashboard de logs")

app.layout = html.Div(
    className="page",
    children=[
        html.H2("Dashboard de logs"),
        html.Div(
            className="layout",
            children=[
                # Columna izquierda
                html.Div(
                    className="sidebar",
                    children=[
                        html.Div(
                            className="controls",
                            children=[
                                html.Div([
                                    html.Label("Servicio"),
                                    dcc.Dropdown(
                                        id="service",
                                        options=[{"label": "Todos", "value": "ALL"}]
                                        + [{"label": s, "value": s} for s in services],
                                        value="ALL",
                                        clearable=False,
                                    ),
                                ]),
                                html.Div([
                                    html.Label("Rango"),
                                    dcc.RadioItems(
                                        id="range",
                                        options=[{"label": k, "value": v} for k, v in RANGES.items()],
                                        value=1,
                                        inline=True,
                                    ),
                                ]),
                                html.Div([
                                    html.Label("Actualizar cada"),
                                    dcc.RadioItems(
                                        id="rate",
                                        options=[{"label": f"{s} s", "value": s} for s in REFRESH_OPTIONS],
                                        value=REFRESH_SECONDS,
                                        inline=True,
                                    ),
                                ]),
                            ],
                        ),
                        dcc.Interval(id="refresh", interval=REFRESH_SECONDS * 1000, n_intervals=0),
                        html.Div(id="kpis"),
                    ],
                ),
                # Columna derecha: gráficas
                html.Div(
                    className="grid",
                    children=[
                        dcc.Graph(id="fig-response"),
                        dcc.Graph(id="fig-percentiles"),
                        dcc.Graph(id="fig-throughput"),
                        dcc.Graph(id="fig-errors"),
                    ],
                ),
            ],
        ),
    ],
)


@app.callback(
    Output("kpis", "children"),
    Output("fig-response", "figure"),
    Output("fig-percentiles", "figure"),
    Output("fig-throughput", "figure"),
    Output("fig-errors", "figure"),
    Input("refresh", "n_intervals"),
    Input("service", "value"),
    Input("range", "value"),
)
def update(_, service, hours):
    try:
        totals = get_totals(service, hours)
        series = get_series(service, hours)
    except psycopg2.Error as exc:
        error = html.Div(f"Error consultando la base de datos: {exc}", style={"color": "red"})
        return error, empty_fig(), empty_fig(), empty_fig(), empty_fig()

    requests = int(totals["requests"])
    kpis = html.Div(
        className="cards",
        children=[
            card("Tiempo de respuesta promedio", fmt_ms(totals["avg_ms"])),
            card("Throughput promedio (RPM)", f"{requests / (hours * 60):,.1f}"),
            card("Errores 4xx", f"{int(totals['err_4xx']):,}"),
            card("Errores 5xx", f"{int(totals['err_5xx']):,}"),
            card("p95", fmt_ms(totals["p95_ms"])),
            card("p99", fmt_ms(totals["p99_ms"])),
        ],
    )

    if series.empty:
        return kpis, empty_fig(), empty_fig(), empty_fig(), empty_fig()

    fig_response = px.line(
        series, x="bucket", y="avg_ms",
        title="Tiempo de respuesta promedio",
        labels={"bucket": "Hora", "avg_ms": "ms"},
    )
    fig_percentiles = px.line(
        series, x="bucket", y=["p95_ms", "p99_ms"],
        title="p95 y p99 response time",
        labels={"bucket": "Hora", "value": "ms", "variable": "Percentil"},
    )
    fig_throughput = px.bar(
        series, x="bucket", y="rpm",
        title="Throughput (requests por minuto)",
        labels={"bucket": "Hora", "rpm": "RPM"},
    )
    fig_errors = px.bar(
        series, x="bucket", y=["err_4xx", "err_5xx"], barmode="stack",
        title="Errores 4xx y 5xx",
        labels={"bucket": "Hora", "value": "Errores", "variable": "Tipo"},
    )
    return kpis, fig_response, fig_percentiles, fig_throughput, fig_errors


@app.callback(Output("refresh", "interval"), Input("rate", "value"))
def set_refresh(seconds):
    # Cambia cada cuánto se ejecuta update(). Dash pide milisegundos.
    return seconds * 1000


if __name__ == "__main__":
    app.run(debug=True)
