import time
import pandas as pd
import plotly.graph_objects as go
import importlib
import logging

from .Time_Series import Time_Series_Class
from Error_Handling import CustomAPIError
from Error_Handling import (
    CashConversionError,
    AssetTypeError, AssetConversionError,
    InvalidAssetFormatError, StockConversionError)

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
        ----------- z 
        asset_name : str
            The name or identifier of the asset.
        """
        super().__init__(asset_name=asset_name)
        if self.debug:
            logging.info("Generating an Asset Class...")

        self.save_path = 'HTML_Diagrams/'
        self.bool_to_save_paths = False

        self.price_data_frame = self._generate_asset_info()
        self.price_vector = self.price_data_frame[['Volume_Weighted']]
        self.at_the_money = int(self.price_vector.iloc[-1])

    def generate_price_data(self):
        """Generates price data and volume-weighted price vectors."""
        self.price_data_frame = self._generate_asset_info()
        self.price_vector = self.price_data_frame[['Volume_Weighted']]
        self.at_the_money = int(self.price_vector.iloc[-1])

    def _get_sub_class(self):
        """
        Dynamically determine and convert the instance into a subclass based on asset name prefixes.
        Falls back to Stock_Class or Cash_Class as necessary.
        """
        try:
            # Map prefixes to asset types
            asset_type_map = {
                'O:': 'Option',
                'X:': 'Crypto',
                'C:': 'Forex',
                'I:': 'Index'
            }
            print("structure")

            # Determine asset type based on prefix
            self.asset_type = asset_type_map.get(self.asset_name[:2])
            acceptable_classes = {"Option", "Crypto", "Forex", "Index"}

            # Validate asset type
            if self.asset_type not in acceptable_classes:
                raise AssetTypeError(
                    message=f"Asset type not recognized: {self.asset_name}. Attempting fallback to Stock class.",
                    asset_type=self.asset_name,
                    sub_error=True
                )

            if self.debug:
                logging.info(f"Attempting to convert to {self.asset_type} class...")

            # Dynamically import the appropriate module and class
            asset_module = importlib.import_module(f'Financial_Products.{self.asset_type}')
            asset_class = getattr(asset_module, f'{self.asset_type}_Class')

            # Convert to the appropriate class
            self.__class__ = asset_class
            asset_class.__init__(self, self.asset_name)

            if self.debug:
                logging.info(f"Converted to {self.__class__.__name__} successfully.")

        except (ModuleNotFoundError, ImportError, AttributeError) as e:
            # Handle invalid module or class
            raise InvalidAssetFormatError(
                asset_data=self.asset_name,
                message=f"Failed to import module or class for {self.asset_type}.",
                details={"error": repr(e)}
            )

        except AssetTypeError as e:
            # Handle unrecognized asset types by falling back to Stock_Class
            logging.warning(str(e))
            try:
                from .Stock import Stock_Class
                self.__class__ = Stock_Class
                Stock_Class.__init__(self, self.asset_name)
                if self.debug:
                    logging.info("Converted to Stock_Class as a fallback.")
            except (TypeError, AttributeError) as stock_error:
                raise StockConversionError(
                    asset_name=self.asset_name,
                    message="Failed to convert to Stock_Class.",
                    details={"error": repr(stock_error)}
                )

        except StockConversionError as e:
            # Handle failed conversion to Stock_Class by falling back to Cash_Class
            logging.warning(str(e))
            try:
                from .Cash import Cash_Class
                self.__class__ = Cash_Class
                Cash_Class.__init__(self, self.asset_name)
                if self.debug:
                    logging.info("Converted to Cash_Class as the final fallback.")
            except (TypeError, AttributeError) as cash_error:
                raise CashConversionError(
                    asset_name=self.asset_name,
                    message="Failed to convert to Cash_Class.",
                    details={"error": repr(cash_error)}
                    )

        except Exception as e:
            # Handle any other unexpected errors
            logging.error(f"Unexpected error during subclass conversion: {repr(e)}")
            raise AssetConversionError(
                asset_name=self.asset_name,
                target_class=self.asset_type,
                message="Failed to convert asset to subclass.",
                details={"error": repr(e)}
            )

    def _generate_asset_info(self) -> pd.DataFrame:
        """
        Fetches asset price information from an API and organizes it into a DataFrame.

        Returns:
        --------
        pd.DataFrame:
            DataFrame containing organized asset price data.
        """
        retry_count = 0
        max_retries = 3

        while retry_count < max_retries:
            if self.debug:
                logging.info(f"Retrying API request... Attempt {retry_count + 1}/{max_retries}")
            try:
                response = self.generate_request()
                break
            except CustomAPIError as e:
                logging.error(f"{repr(e)} Retry {retry_count + 1}/{max_retries}...")
                retry_count += 1
                time.sleep(15)  # Optional delay for retries

        result = pd.DataFrame(response['results'])

        result['Time'] = pd.to_datetime(result.pop('t'), unit='ms', utc=True)
        result['Volume_Weighted'] = result.pop('vw')
        result['Open_Price'] = result.pop('o')
        result['Lowest_Price'] = result.pop('l')
        result['Highest_Price'] = result.pop('h')
        result['Close_Price'] = result.pop('c')
        result['Volume'] = result.pop('v')

        return result.set_index('Time')

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
        fig.show()
        if self.bool_to_save_paths:
            fig.write_html(f'{self.save_path}{ticker}_time_series.html')
