import time
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
import importlib
import logging
from .Time_Series import Time_Series_Class
from Error_Handling.Error_Types import APIError
import warnings

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


class Asset_Class(Time_Series_Class):
    """
    A class to represent and manage asset data for a given financial asset.

    Attributes:
    -----------
    asset_name : str
        The name of the financial asset.
    asset_time_series : Time_Series_Class
        An instance for time series data management.
    model_asset : bool
        Indicates whether the asset can be modeled.

    Methods:
    --------
    _generate_price_data():
        Generates price and volume-weighted data for the asset.

    _get_sub_class():
        Dynamically determines and converts the asset instance to a subclass based on its type.

    _generate_asset_info():
        Generates and returns asset price information from an API.

    plot_time_series():
        Plots the candlestick chart and volume-weighted price of the asset.
    """

    def __init__(self, asset_name: str):
        """
        Initialize the Asset_Class with asset name and time series data.

        Parameters:
        -----------
        asset_name : str
            The name or identifier of the asset.
        """
        logging.info("Generating an Asset Class...")
        self.asset_name = asset_name
        self.asset_time_series = Time_Series_Class(asset_name)
        self.model_asset = True

    def _generate_price_data(self):
        """Generates price data and volume-weighted price vectors."""
        self.price_data_frame = self._generate_asset_info()
        self.price_vector = self.price_data_frame[['Volume_Weighted']]
        self.at_the_money = int(self.price_vector.iloc[-1])

    def _get_sub_class(self):
        """
        Dynamically determine and convert the instance into a subclass based on asset name prefixes.
        Falls back to Stock_Class or Cash_Class as necessary.
        """
        asset_type_map = {
            'O:': 'Option',
            'X:': 'Crypto',
            'C:': 'Forex',
            'I:': 'Index'
        }
        self.asset_type = asset_type_map.get(self.asset_name[:2])

        try:
            if self.asset_type:
                asset_module = importlib.import_module(f'Financial_Products.{self.asset_type}')
                asset_class = getattr(asset_module, f'{self.asset_type}_Class')
                self.__class__ = asset_class
                asset_class.__init__(self, self.asset_name)
                logging.info(f"Converted to {self.__class__.__name__} successfully.")
            else:
                from .Stock import Stock_Class
                self.__class__ = Stock_Class
                Stock_Class.__init__(self, self.asset_name)
                logging.info(f"Converted to Stock_Class as a fallback.")
        except ValueError:
            from .Cash import Cash_Class
            self.__class__ = Cash_Class
            Cash_Class.__init__(self, self.asset_name)
            logging.info(f"Converted to Cash_Class as the final fallback.")

    def _generate_asset_info(self) -> pd.DataFrame:
        """
        Fetches asset price information from an API and organizes it into a DataFrame.

        Returns:
        --------
        pd.DataFrame:
            DataFrame containing organized asset price data.
        """
        response = self.asset_time_series.api_object.generate_request()
        retry_count = 0
        max_retries = 3

        while retry_count < max_retries:
            try:
                if response.get('status') == 'ERROR':
                    raise APIError(response)
                elif response.get('status') == 'DELAYED' and response.get('resultsCount') == 0:
                    logging.warning("Response delayed. Retrying...")
                    time.sleep(13)
                    retry_count += 1
                    continue

                for result in response['results']:
                    result['Time'] = pd.to_datetime(result.pop('t'), unit='ms', utc=True)
                    result['Volume_Weighted'] = result.pop('vw')
                    result['Open_Price'] = result.pop('o')
                    result['Lowest_Price'] = result.pop('l')
                    result['Highest_Price'] = result.pop('h')
                    result['Close_Price'] = result.pop('c')
                    result['Volume'] = result.pop('v')

                return pd.DataFrame(response['results']).set_index('Time')

            except APIError as e:
                logging.error(f"API Error: {repr(e)}. Retrying...")
                time.sleep(10)
                retry_count += 1

        raise APIError("Max retries reached. Failed to fetch asset data.")

    def _plot_time_series(self):
        """
        Plots the asset's time series data as a candlestick chart with volume-weighted prices.
        """
        if self.asset_name[1] == ':':
            ticker = self.asset_name[2:]
        else:
            ticker = self.asset_name

        fig = go.Figure(data=[
            go.Candlestick(x=self.price_data_frame.index,
                           open=self.price_data_frame['Open_Price'],
                           high=self.price_data_frame['Highest_Price'],
                           low=self.price_data_frame['Lowest_Price'],
                           close=self.price_data_frame['Close_Price'],
                           name='Candlestick'),
            go.Scatter(x=self.price_data_frame.index,
                       y=self.price_data_frame['Volume_Weighted'],
                       mode='lines', name='Volume Weighted', line=dict(color='yellow',dash='dot'))
        ])

        fig.update_layout(title=f'{ticker} Time Series Data',
                          xaxis_title='Date',
                          yaxis_title='Price',
                          template='plotly_dark')
        fig.write_html(f'{ticker}_time_series.html')
        fig.show()
