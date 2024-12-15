from Financial_Products.Time_Series import Time_Series_Class
import pandas as pd
import plotly.express as px
from datetime import datetime
import yfinance as yf

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
        self.asset_label = asset_name
        self.asset_name = asset_name

        print(len(self.asset_name))
        if len(self.asset_name) > 5:
            self.asset_name = f'O:{asset_name}'
        else:
            self.option_data_frame = self.generate_option_ticker()
            self.call_options = self.option_data_frame['Calls']
            self.put_options = self.option_data_frame['Puts']


        self.asset_time_series = Time_Series_Class(asset_name)
        self.price_data_frame = self.generate_asset_info()

        try:
            self.price_vector = self.price_data_frame[['Time','Volume_Weighted']]
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')




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

            self.organized_data = pd.DataFrame(response['results'])
            return self.organized_data
        except:
            return f'There was an error with the API request of type'
    def generate_option_ticker(self):
        try:
            ticker_list = yf.Ticker(self.asset_name)
            master_dict = {"Calls": {}, "Puts": {}}
            for i in range(0, len(ticker_list.options)):
                master_dict["Calls"][ticker_list.options[i]] = {}
                master_dict["Puts"][ticker_list.options[i]] = {}
            for j in master_dict["Calls"].keys():

                call_df_for_date = ticker_list.option_chain(date=j).calls
                puts_df_for_date = ticker_list.option_chain(date=j).puts

                for k in range(0, len(call_df_for_date['strike'])):
                    master_dict["Calls"][j][call_df_for_date['strike'][k]] = call_df_for_date['contractSymbol'][k]
                for k in range(0, len(puts_df_for_date['strike'])):
                    master_dict["Puts"][j][puts_df_for_date['strike'][k]] = puts_df_for_date['contractSymbol'][k]

            return {'Calls': pd.DataFrame(master_dict['Calls']), 'Puts': pd.DataFrame(master_dict['Puts'])}
        except IndexError as e:
            print(f'Error on retrieving option ticker. Error type {e}')
            return {'Calls': [], 'Puts': []}
    def plot_time_series(self, start_date = datetime(2023,1,1), end_date = datetime.today()):

        fig = px.line(self.price_data_frame, x = 'Time', y = 'Volume_Weighted',
                      title = self.asset_label)
        fig.show()
