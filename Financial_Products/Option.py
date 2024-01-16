from requests import request
from datetime import datetime

from Derivative_Trading_Strategy.Utilities.Utilities_Resources import apiKey
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import headers
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import payload


from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series
class Option(Time_Series):

    def __init__(self, ticker):

        self.option_time_series = Time_Series(ticker)
        self.option_info = self.option_time_series.response

        self.value = self.option_info['Volume_Weighted']
        print(self.value)
