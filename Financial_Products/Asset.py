from requests import request
from datetime import datetime
import pandas as pd
from Derivative_Trading_Strategy.Utilities.API_Connection import API_Connection
import yfinance as yf

from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series

class Asset(Time_Series):

    def __init__(self, asset_name):
        print('Generating an Asset Class')
        self.asset_name = asset_name

        if len(asset_name) > 5:
            self.asset_name = f'O:{asset_name}'

        self.asset_time_series = Time_Series(asset_name)


        self.price_data_frame = self.generate_asset_info()


        try:
            self.price_vector = self.price_data_frame[['Volume_Weighted']]
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')

        self.option_data_frame = self.generate_option_ticker()
        self.call_options = self.option_data_frame['Calls']
        self.put_options = self.option_data_frame['Puts']


    def generate_asset_info(self):

        response = self.asset_time_series.api_object.generate_request()

        # Adjust the MS timespan to proper time
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

        return {'Calls' : pd.DataFrame(master_dict['Calls']), 'Puts' : pd.DataFrame(master_dict['Puts'])}

