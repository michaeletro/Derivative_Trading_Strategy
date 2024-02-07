from requests import request
from datetime import datetime

from Derivative_Trading_Strategy.Utilities.Utilities_Resources import apiKey
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import headers
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import payload


from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Option(Asset):

    def __init__(self, asset_name):

        self.asset_time_series = Asset(asset_name)

        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')

        self.option_data_frame = self.asset_time_series.generate_asset_info()

#o = Option('AAPL240119C00050000')

#print(o.option_data_frame)
