from requests import request
from datetime import datetime

from Derivative_Trading_Strategy.Utilities.Utilities_Resources import apiKey
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import headers
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import payload


from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Option(Asset):

    def __init__(self, ticker):

        self.option_time_series = Asset(ticker)
        try:
            self.price_data_frame = self.option_time_series.price_data_frame
            self.price_vector = self.option_time_series.price_vector
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')

o = Option('O:AAPL240119C00050000')

print(o.price_data_frame)
print(o.generate_asset_info())