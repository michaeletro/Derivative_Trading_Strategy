from dash import Dash
import dash_bootstrap_components as dbc
from app.layout import layout  # Import the layout
from app.callbacks import register_callbacks  # Import the function to register callbacks

# Initialize Dash App
app = Dash(__name__, external_stylesheets=[dbc.themes.DARKLY, 
                                           "https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css",
                                           "/assets/styles.css"])
app.title = "Quant Dashboard"
app.layout = layout  # Make sure `layout` is correctly imported

# Register Callbacks
register_callbacks(app)  # Pass `app` instance

# Run server
if __name__ == "__main__":
    app.run_server(debug=True)
