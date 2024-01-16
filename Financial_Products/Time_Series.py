from requests import request
from datetime import datetime
import pandas as pd
from Derivative_Trading_Strategy.Utilities.API_Connection import API_Connection

class Time_Series(API_Connection):

    def __init__(self, ticker, start_date="2023-01-09", end_date=datetime.today().strftime("%Y-%m-%d"), time_multiplier="1"):

        self.ticker = ticker
        self.start_date = start_date
        self.end_date = end_date
        self.time_multiplier = time_multiplier

        self.api_object = API_Connection(asset=ticker, time_multiplier=self.time_multiplier, start_date=self.start_date,
                                         end_date=self.end_date)

        self.response = self.generate_asset_info()
    def __iter__(self):
        self.current_index = 0
        return self

    def generate_asset_info(self):

        response = self.api_object.response

        # Adjust the MS timespan to proper time
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

    def __setattr__(self, value_replacing, value_to_be_replaced):
        super().__setattr__(value_replacing, value_to_be_replaced)
    #    self.response = self.generate_asset_info()

