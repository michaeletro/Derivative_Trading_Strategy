import dash
from dash import dcc, html, Input, Output, dash_table, State
import plotly.express as px
import pandas as pd
import io
import base64
import os

# Initialize Dash App
app = dash.Dash(__name__, external_stylesheets=["https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css"])
app.title = "Live Financial Data Dashboard"

# Helper Function to Load CSV Data
def load_csv(file_path):
    if os.path.exists(file_path):
        return pd.read_csv(file_path)
    else:
        return pd.DataFrame({
            "timestamp": [], "open_price": [], "close_price": [],
            "highest_price": [], "lowest_price": [], "volume": []
        })

# Initial Data Load (replace 'data.csv' with your CSV file path)
CSV_PATH = "data.csv"
df = load_csv(CSV_PATH)

# App Layout
app.layout = html.Div([
    html.Div([
        html.H1("Live Financial Data Dashboard", className="text-center my-4"),

        # File Upload Section
        html.Div([
            html.Label("Upload CSV File", className="form-label"),
            dcc.Upload(
                id="upload_csv",
                children=html.Div(["Drag and Drop or ", html.A("Select a File")]),
                style={
                    "width": "100%", "height": "60px", "lineHeight": "60px",
                    "borderWidth": "1px", "borderStyle": "dashed",
                    "borderRadius": "5px", "textAlign": "center",
                    "marginBottom": "20px",
                },
                multiple=False,
            ),
        ], className="container"),

        # Dropdown to Select Metric
        html.Div([
            dcc.Dropdown(
                id="metric_dropdown",
                options=[
                    {"label": "Open Price", "value": "open_price"},
                    {"label": "Close Price", "value": "close_price"},
                    {"label": "Highest Price", "value": "highest_price"},
                    {"label": "Lowest Price", "value": "lowest_price"},
                    {"label": "Volume", "value": "volume"}
                ],
                value="close_price",
                multi=False,
                clearable=False,
                placeholder="Select a metric",
                className="mb-3",
            ),
        ], className="container"),

        # Chart Section
        html.Div([
            dcc.Graph(id="line_chart"),
        ], className="container mb-4"),

        # Data Table
        html.Div([
            html.Label("Data Table", className="form-label"),
            dash_table.DataTable(
                id="data_table",
                columns=[],
                data=[],
                style_table={"overflowX": "auto"},
                style_header={"backgroundColor": "rgb(230, 230, 230)", "fontWeight": "bold"},
                style_cell={"textAlign": "center", "padding": "5px"},
            ),
        ], className="container"),
    ]),

    # Interval Component for Live Updates
    dcc.Interval(
        id="interval_component",
        interval=10*1000,  # 10 seconds
        n_intervals=0
    )
])

# Callbacks
@app.callback(
    [Output("line_chart", "figure"), Output("data_table", "columns"), Output("data_table", "data")],
    [Input("metric_dropdown", "value"), Input("upload_csv", "contents"), Input("interval_component", "n_intervals")],
    [State("upload_csv", "filename")],
)
def update_dashboard(selected_metric, contents, n_intervals, filename):
    global df

    # Handle file upload
    if contents:
        content_type, content_string = contents.split(",")
        decoded = base64.b64decode(content_string)
        df = pd.read_csv(io.StringIO(decoded.decode("utf-8")))

    # Reload the CSV file periodically
    if os.path.exists(CSV_PATH):
        df = load_csv(CSV_PATH)

    # Ensure metric exists in data
    if selected_metric not in df.columns:
        return px.line(title="No Data Available"), [], []

    # Update line chart
    fig = px.line(
        df,
        x="timestamp",
        y=selected_metric,
        title=f"{selected_metric.replace('_', ' ').title()} Over Time",
        markers=True,
    )
    fig.update_layout(
        template="plotly_white",
        xaxis_title="Timestamp",
        yaxis_title=selected_metric.replace("_", " ").title(),
    )

    # Update table
    table_columns = [{"name": col, "id": col} for col in df.columns]
    table_data = df.to_dict("records")

    return fig, table_columns, table_data


# Run the app
if __name__ == "__main__":
    app.run_server(debug=True)
