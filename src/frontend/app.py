
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import dash_extensions as de
import json
import plotly.express as px
import requests
import pandas as pd

# Initialize Dash app
# Initialize Dash App
app = dash.Dash(__name__, external_stylesheets=["https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css"])
app.title = "Live Financial Data Dashboard"

# App layout
app.layout = html.Div([
    
    html.H1("Quantitative Dashboard (WebSocket)", style={"textAlign": "center"}),
    dcc.Tabs([
        dcc.Tab(label="Live Portfolio", children=[
            dcc.Graph(id="live-portfolio-graph"),
            de.WebSocket(id="live-portfolio-ws", url="ws://127.0.0.1:8081/live_portfolio")
        ]),
        dcc.Tab(label="Time Series Analysis", children=[
            dcc.Graph(id="time-series-graph"),
            de.WebSocket(id="time-series-ws", url="ws://127.0.0.1:8081/time_series")
        ])
    ]),
    html.H1("IDK Trynna fetch data or something, using some rest API or something", style={"textAlign": "center"}),
    html.Button("Fetch Data from C++ Server BECAUSE a real server is expensive", id="fetch-data-btn"),
    html.Div(id="server-response"),
    dcc.Interval(id="data-interval", interval=1000, n_intervals=0),  # Polling interval
])


# Callbacks
@app.callback(
    Output("server-response", "children"),
    Input("fetch-data-btn", "n_clicks"),
    prevent_initial_call=True
)
def fetch_data(n_clicks):
    try:
        # Make a GET request to the C++ REST API
        response = requests.get("http://localhost:8080/api/data")
        response.raise_for_status()  # Check for errors

        # Convert JSON response to a string or dataframe
        data = response.json()
        return f"Response from server: {data['data']}"

    except requests.RequestException as e:
        return f"Error connecting to server: {e}"


# Callback to update the live portfolio graph using WebSocket
@app.callback(
    Output("live-portfolio-graph", "figure"),
    [Input("live-portfolio-ws", "message")]
)
def update_live_portfolio_graph(message):
    if message:
        data = json.loads(message)
        df = pd.DataFrame.from_dict(data["assets"])
        fig = px.line(df, x="name", y="value", title="Live Portfolio Value")
        fig.update_layout(template="plotly_dark")
        return fig
    return {}

# Callback to update the time series graph using WebSocket
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

# Run the app
if __name__ == "__main__":
    app.run_server(debug=True)
