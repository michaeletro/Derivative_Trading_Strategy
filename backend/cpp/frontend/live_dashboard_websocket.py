
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import dash_extensions as de
import json
import plotly.express as px
import pandas as pd

# Initialize Dash app
app = dash.Dash(__name__)
app.title = "Quantitative Dashboard with WebSocket"

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
    ])
])

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
