
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import pandas as pd
import plotly.express as px
import requests

# Initialize Dash app
app = dash.Dash(__name__)
app.title = "Quantitative Dashboard"

# App layout
app.layout = html.Div([
    html.H1("Quantitative Research Dashboard", style={"textAlign": "center"}),
    dcc.Tabs([
        dcc.Tab(label="Live Portfolio", children=[
            dcc.Graph(id="live-portfolio-graph"),
            dcc.Interval(
                id="live-portfolio-interval",
                interval=1000,  # Update every second
                n_intervals=0
            )
        ]),
        dcc.Tab(label="Time Series Analysis", children=[
            dcc.Graph(id="time-series-graph"),
            html.Button("Fetch Time Series Data", id="fetch-time-series-btn", n_clicks=0)
        ])
    ])
])

# Callback to update the live portfolio graph
@app.callback(
    Output("live-portfolio-graph", "figure"),
    [Input("live-portfolio-interval", "n_intervals")]
)
def update_live_portfolio_graph(n):
    try:
        # Fetch live portfolio data from C++ backend
        response = requests.get("http://127.0.0.1:8080/live_portfolio")
        response.raise_for_status()
        data = pd.DataFrame.from_dict(response.json()["assets"])
        
        # Create the figure
        fig = px.line(data, x="name", y="value", title="Live Portfolio Value")
        fig.update_layout(template="plotly_dark")
        return fig
    except Exception as e:
        print(f"Error fetching portfolio data: {e}")
        return {}

# Callback to fetch and display time series data
@app.callback(
    Output("time-series-graph", "figure"),
    [Input("fetch-time-series-btn", "n_clicks")]
)
def fetch_time_series_data(n_clicks):
    if n_clicks > 0:
        try:
            # Fetch time series data from C++ backend
            response = requests.get("http://127.0.0.1:8080/time_series")
            response.raise_for_status()
            data = pd.DataFrame.from_dict(response.json()["priceData"], orient="index")
            data.reset_index(inplace=True)
            data.columns = ["Timestamp", "Price"]
            
            # Create the graph
            fig = px.line(data, x="Timestamp", y="Price", title="Time Series Data")
            fig.update_layout(template="plotly_dark")
            return fig
        except Exception as e:
            print(f"Error fetching time series data: {e}")
            return {}

# Run the app
if __name__ == "__main__":
    app.run_server(debug=True)
