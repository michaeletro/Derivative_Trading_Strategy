from requests import request
from datetime import datetime

from Derivative_Trading_Strategy.Utilities.Utilities_Resources import apiKey
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import headers
from Derivative_Trading_Strategy.Utilities.Utilities_Resources import payload

from Derivative_Trading_Strategy.Financial_Products.Portfolio import Portfolio

class Forex(Portfolio):
    def __int__(self, asset):
        self.asset = asset