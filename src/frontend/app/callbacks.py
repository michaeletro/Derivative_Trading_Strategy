import json
import requests
import pandas as pd
import plotly.express as px
from dash import Input, Output, dcc, html
import dash_bootstrap_components as dbc
import dash_extensions as de

def register_callbacks(app):
    """Register all Dash callbacks."""
    @app.callback(
        Output("server-response", "children"),
        Input("fetch-data-btn", "n_clicks"),
        prevent_initial_call=True
    )
    def fetch_data(n_clicks):
        try:
            # Show loading animation while fetching data
            loading_msg = html.Div("Fetching Data...", className="loading-text fade-in")

            # Make a GET request to the C++ server API
            response = requests.get("http://localhost:8080/api/asset_data")
            response.raise_for_status()

            # Parse JSON response
            data = response.json()

            # 🔥 Handle the "No Data Available" case with fixed height
            if "data" not in data or not data["data"]:
                return html.Div(
                    className="no-data-container fade-in",  # Keeps the height stable
                    children=[
                        html.Div("⚠️ No Data Available", className="no-data-text"),
                        html.P("Try fetching again or check your connection.", className="text-muted"),
                    ],
                )

            # Convert JSON data into an HTML table
            table_header = [html.Thead(html.Tr([html.Th(col) for col in data["data"][0].keys()]))]
            table_body = [html.Tbody([html.Tr([html.Td(str(entry[col])) for col in entry]) for entry in data["data"]])]

            table = html.Table(children=table_header + table_body, className="data-table")

            return html.Div([
                html.H5("📊 Data from C++ Backend:", className="data-header"),
                table
            ], className="data-container fade-in")

        except requests.RequestException as e:
            return html.Div(
                className="data-container fade-in",  # Keeps the height stable
                children=[
                    html.Div(f"Error fetching data: {str(e)}", className="error-text"),
                ],
            )



    @app.callback(
        Output("live-portfolio-graph", "figure"),
        [Input("live-portfolio-ws", "message")]
    )
    def update_live_portfolio_graph(message):
        print("Received WebSocket Message:", message)  # Debugging Log

        if not message:
            print("No message received")
            return {}

        try:
            data = json.loads(message)
            print("Parsed JSON:", data)

            df = pd.DataFrame.from_dict(data["assets"])
            print("DataFrame:", df)

            fig = px.line(df, x="name", y="value", title="Live Portfolio Value")
            fig.update_layout(template="plotly_dark")

            return fig

        except Exception as e:
            print("Error in Callback:", e)
            return {}

    @app.callback(
        Output("time-series-graph", "figure"),
        [Input("time-series-ws", "message")]
    )
    def update_time_series_graph(message):
        if message:
            data = json.loads(message)
            df = pd.DataFrame.from_dict(data["priceData"], orient="index")
            df.reset_index(inplace=True)
            df.columns = ["Timestamp", "Price"]
            fig = px.line(df, x="Timestamp", y="Price", title="Time Series Data")
            fig.update_layout(template="plotly_dark")
            return fig
        return {}
    # Custom Loading Spinner
    loading_spinner = html.Div(
        className="d-flex justify-content-center align-items-center fade-in",
        children=[
            html.Div(className="spinner-border text-primary loading-icon", role="status"),
            html.Span(" Loading...", className="ms-2 fs-5 text-white"),
        ]
    )

    @app.callback(
        Output("tab-content", "children"),
        Input("tabs", "active_tab")
    )
    def switch_tabs(active_tab):
        if active_tab == "live-portfolio-tab":
            return html.Div(className="card bg-secondary p-4 shadow-lg fade-in", children=[
                dcc.Graph(id="live-portfolio-graph"),
                de.WebSocket(id="live-portfolio-ws", url="ws://127.0.0.1:8081/live_portfolio")
            ])
        elif active_tab == "time-series-tab":
            return html.Div(className="card bg-secondary p-4 shadow-lg fade-in", children=[
                dcc.Graph(id="time-series-graph"),
                de.WebSocket(id="time-series-ws", url="ws://127.0.0.1:8081/time_series")
            ])
        return loading_spinner  # Default: Show loading animation

