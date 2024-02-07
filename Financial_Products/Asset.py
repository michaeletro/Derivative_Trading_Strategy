import datetime
from datetime import datetime
import pandas as pd
import plotly
import plotly.express as px

from Derivative_Trading_Strategy.Utilities.API_Connection import API_Connection
import yfinance as yf

from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series

class Asset(Time_Series):

    def __init__(self, asset_name):
        print('Generating an Asset Class')
        self.asset_name = asset_name

        # This length clause implies that is an Option type, kinds wonky but will fix
        # later.
        if len(asset_name) > 5:
            self.asset_name = f'O:{asset_name}'
        else:
            self.option_data_frame = self.generate_option_ticker()
            self.call_options = self.option_data_frame['Calls']
            self.put_options = self.option_data_frame['Puts']

        self.asset_time_series = Time_Series(asset_name)
        self.price_data_frame = self.generate_asset_info()


        try:
            self.price_vector = self.price_data_frame[['Volume_Weighted']]
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')


    def generate_asset_info(self):
        #print('---------')
        #print(self.asset_time_series.api_object.generate_request())
        response = self.asset_time_series.api_object.generate_request()
        #print(response)
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

            print(pd.DataFrame(response['results']))
            self.organized_data = pd.DataFrame(response['results'])
            return self.organized_data
        except:
            return f'There was an error with the API request of type'
            print(0)

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

            return {'Calls' : pd.DataFrame(master_dict['Calls']), 'Puts' : pd.DataFrame(master_dict['Puts'])}
        except TypeError as e:
            print(f'Error on retrieving option ticker. Error type {e}')
            return {'Calls' : [], 'Puts' : []}

    def plot_time_series(self, start_date = datetime(2023,1,1), end_date = datetime.today()):
        print(colnames(self.price_data_frame))
        fig = px.line(self.price_data_frame, x = 'Time', y = 'Volume',
                      title = self.asset_name)
        fig.show()
        print(self.price_data_frame)

#Temp_A = Asset('AAPL')

#Temp_A.plot_time_series()


