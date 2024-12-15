import pandas as pd
import plotly.express as px
from datetime import datetime
import re

from Financial_Products.Time_Series import Time_Series_Class
from Financial_Products.Cash import Cash_Class
from Financial_Products.Crypto import Crypto_Class
from Financial_Products.Forex import Forex_Class
from Financial_Products.Indices import Index_Class
from Financial_Products.Option import Option_Class
from Financial_Products.Stock import Stock_Class

class Asset_Class(Time_Series_Class):
    """
    This module defines the `Asset_Class` for managing asset data in the financial products package.

    Classes:
    --------
    Asset_Class(Time_Series_Class):
        A class to represent and manage asset data for a given financial asset.

        Methods:
        --------
        __init__(self, asset_name):
            Initializes the Asset_Class with the specified asset name and creates a Time_Series_Class instance.

        generate_asset_info(self):
            Generates and returns a DataFrame containing organized asset information from the API response.

        generate_option_ticker(self):
            Generates and returns a dictionary containing call and put option tickers for the asset.

        plot_time_series(self, start_date=datetime(2023,1,1), end_date=datetime.today()):
            Plots the time series data of the asset using Plotly.
    """
    def __init__(self, asset_name):
        print('Generating an Asset Class')
        # Initialize the Asset_Class with the specified asset name
        self.asset_time_series = Time_Series_Class(asset_name)
        self.model_asset = True
        self.asset_name = asset_name

        # Determine the asset type based on the asset name
        self.asset_type = {'O:': Option_Class(asset_name),
                           'X:': Crypto_Class(asset_name),
                           'C:': Forex_Class(asset_name),
                           'I:': Index_Class(asset_name)}.get(asset_name[:2])

        # If the asset type is not found, default to Stock_Class
        # If asset type fails Stock_Class, default to Cash_Class
        if self.asset_type is None:
            try:
                self.asset_type = Stock_Class(asset_name)
            except ValueError:
                self.asset_type = Cash_Class(asset_name)
                self.model_asset = False
                print(f'Asset name {asset_name} is not in the dictionary and is not a Stock.'
                      f'Defaulting to Cash_Class')

        # Generate asset information based on the asset type
        if self.model_asset:

            # Generate the asset information DataFrame
            self.price_data_frame = self.generate_asset_info()
            self.price_vector = pd.Series(self.price_data_frame['Volume_Weighted'], index=self.price_data_frame['Time'])
            self.at_the_money = int(self.price_vector['Volume_Weighted'].iloc[-1])

            if isinstance(self.asset_type, Stock_Class):
                # Generate the option ticker information
                self.option_info = self.generate_option_ticker()
                self.call_options = self.option_info['Calls']
                self.put_options = self.option_info['Puts']
            if isinstance(self.asset_type, Option_Class):
                # Parse the expiration date and option type
                self.expiration_date, self.option_type = self.parse_expiration_date()
                self.strike_price = self.at_the_money
            if isinstance(self.asset_type, Crypto_Class):
                print(3)
            if isinstance(self.asset_type, Forex_Class):
                # Extract the currency pair, numerator, and denominator
                self.currency_pair = self.asset_name[2:]
                self.currency_numerator = self.currency_pair[2:4]
                self.currency_denominator = self.currency_pair[5:7]
            if isinstance(self.asset_type, Index_Class):
                print(5)
        else:
            # If the asset is not modeled, set the attributes to None
            self.price_data_frame = []
            self.price_vector = []
            self.at_the_money = None
            self.option_info = None
            self.call_options = []
            self.put_options = []
            self.expiration_date = None
            self.strike_price = None

    # Generate asset information from the API response
    def generate_asset_info(self):
        response = self.asset_time_series.api_object.generate_request()
        try:
            for i in range(0, len(response['results'])):
                response['results'][i]['t'] = pd.to_datetime(response['results'][i]['t'], unit='ms')

                response['results'][i]['Time'] = response['results'][i].pop('t')
                response['results'][i]['Volume_Weighted'] = response['results'][i].pop('vw')
                response['results'][i]['Open_Price'] = response['results'][i].pop('o')
                response['results'][i]['Lowest_Price'] = response['results'][i].pop('l')
                response['results'][i]['Highest_Price'] = response['results'][i].pop('h')
                response['results'][i]['Close_Price'] = response['results'][i].pop('c')
                response['results'][i]['Volume'] = response['results'][i].pop('v')
                response['results'][i]['Lot_Size'] = response['results'][i].pop('n')

            return pd.DataFrame(response['results'])
        except (KeyError, TypeError) as e:
            return f'There was an error with the API request: {e}'

    def plot_time_series(self):
        fig = px.line(self.price_data_frame, x='Time', y='Volume_Weighted',
                      title=self.asset_name + ' Time Series')
        fig.show()

    def generate_option_ticker(self):
        try:
            master_dict = {"Calls": {}, "Puts": {}}
            for date in self.asset_name.options:
                master_dict["Calls"][date] = {}
                master_dict["Puts"][date] = {}
                call_df = self.asset_name.option_chain(date=date).calls
                put_df = self.asset_name.option_chain(date=date).puts
                for strike, symbol in zip(call_df['strike'], call_df['contractSymbol']):
                    master_dict["Calls"][date][strike] = symbol
                for strike, symbol in zip(put_df['strike'], put_df['contractSymbol']):
                    master_dict["Puts"][date][strike] = symbol
            return {'Calls': pd.DataFrame(master_dict['Calls']), 'Puts': pd.DataFrame(master_dict['Puts'])}
        except IndexError as e:
            print(f'Error on retrieving option ticker. Error type {e}')
            return {'Calls': [], 'Puts': []}

    def parse_option_details(self):
        # Define the regular expression pattern to capture the date part and the option type
        pattern = r'O:[A-Z]+(\d{6})([CP])\d+'
        # Search for the pattern in the option string
        match = re.search(pattern, self.asset_name)

        if match:
            # Extract the date part and the option type
            date_str = match.group(1)
            option_type = match.group(2)
            # Convert the date string to a datetime object
            expiration_date = datetime.strptime(date_str, '%y%m%d')
            return expiration_date, option_type
        else:
            raise ValueError("Invalid option string format")
