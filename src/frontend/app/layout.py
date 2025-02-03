from dash import dcc, html
import dash_extensions as de
import dash_bootstrap_components as dbc

# 🎡 Loading Spinner
loading_spinner = html.Div(
    className="d-flex justify-content-center align-items-center fade-in",
    children=[
        html.Div(className="spinner-border text-primary spinner-lg", role="status"),
        html.Span(" Loading...", className="ms-2 fs-5 text-white fw-bold"),
    ],
    style={"height": "100%"}  # Ensures the height doesn't collapse
)

# 🚀 Define the Layout
layout = html.Div(
    className="d-flex flex-column align-items-center bg-dark text-white vh-100",
    children=[
        # 🌟 Header Section
        html.Div(
            className="text-center mb-3",
            children=[
                html.H1("Quantitative Dashboard", className="display-4 fw-bold text-glow"),
                html.P("Live Portfolio & Time Series Analysis", className="lead"),
            ],
            style={"width": "100%"}
        ),

        # 🔵 Modern Navigation Tabs (Fixed at the Top)
        html.Div(
            className="w-100 d-flex justify-content-center",
            children=[
                dbc.Tabs(
                    id="tabs",
                    className="nav nav-tabs modern-tabs p-2",
                    active_tab="live-portfolio-tab",
                    children=[
                        dbc.Tab(label="📈 Live Portfolio", tab_id="live-portfolio-tab", labelClassName="fw-bold"),
                        dbc.Tab(label="📊 Time Series Analysis", tab_id="time-series-tab", labelClassName="fw-bold"),
                        dbc.Tab(label="💰 Portfolio Holdings", tab_id="current-holdings-tab", labelClassName="fw-bold"),
                    ],
                ),
            ],
            style={"width": "100%"}
        ),

        # 📊 Graph Container (Prevents Expanding Too Much)
        html.Div(
            id="tab-content",
            className="glass-card w-100 mt-3 p-4 shadow-lg fade-in",
            children=loading_spinner,
            style={
                "maxWidth": "100%",
                "flexGrow": 1,  # Allows it to fill space dynamically
                "overflow": "hidden"
            }
        ),

        # 🔄 Fetch Data Section (Fixed Size)
        html.Div(
            className="mt-3 text-center glass-card p-3",
            children=[
                html.Button("📡 Fetch Data", id="fetch-data-btn", className="modern-button"),
                html.Div(id="server-response", className="mt-3 fs-5 fade-in text-center"),
            ],
            style={"maxWidth": "100%", "flexGrow": 0, "maxHeight":"100%"}  # Prevents it from expanding
        ),

        # ⏳ Auto Refresh Data Every Second
        dcc.Interval(id="data-interval", interval=1000, n_intervals=0),
    ],
    style={"overflow": "hidden"}
)
