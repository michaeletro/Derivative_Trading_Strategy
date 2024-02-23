import numpy as np
import plotly
import plotly.express as px

from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series
from Derivative_Trading_Strategy.Financial_Products.Stock import Stock
from Derivative_Trading_Strategy.Financial_Products.Option import Option
from Derivative_Trading_Strategy.Financial_Products.Crypto import Crypto
from Derivative_Trading_Strategy.Financial_Products.Forex import Forex
from Derivative_Trading_Strategy.Financial_Products.Indices import Index
from Derivative_Trading_Strategy.Financial_Products.Cash import Cash

class Portfolio(Time_Series):

    def __init__(self, stock_position  = [], option_position = [], cash_position = [], 
                 fx_positions = [], crypto_positions = [], index_positions = [],):
        print('Generating a Portfolio Class')

        self.stock_position = stock_position
        self.option_position = option_position
        self.cash_position = cash_position
        self.fx_positions = fx_positions
        self.crypto_position = crypto_positions
        self.index_positions = index_positions

        self.portfolio_of_assets = {"Stock": self.stock_position, "Option": self.option_position,
                                    "Cash": self.cash_position, "FX": self.fx_positions,
                                    "Crypto": self.crypto_position, "Index":self.index_positions}


    def generate_combined_value(self):

        cumulative_value = []
        for key, value in self.portfolio_of_assets.items():
            for items in value:
                print(value)

                cumulative_value = items.price_vector + cumulative_value
        return cumulative_value

    def generate_portfolio_returns(self):
        cumulative_value = self.generate_combined_value()

        fig = px.line(cumulative_value, y='Volume_Weighted', title = 'Portfolio')
        fig.show()
        print(22)
    def return_new_portfolio(self, asset_to_include, asset_type):
        new_portfolio = {"Stock": self.stock_position, "Option": self.option_position,
                         "Cash": self.cash_position, "FX": self.fx_positions, "Crypto": self.crypto_position,
                         "Index": self.index_positions}

        if isinstance(asset_to_include, Portfolio):
            print(0)

            for key, value in asset_to_include.portfolio_of_assets.items():
                if len(value) == 0:
                    continue
                else:
                    new_portfolio[key].append(value)

            return Portfolio(stock_position=new_portfolio['Stock'], option_position=new_portfolio['Option'],
                             cash_position=new_portfolio['Cash'], fx_positions=new_portfolio['FX'],
                             crypto_positions=new_portfolio['Crypto'], index_positions=new_portfolio['Index'])

        else:
            new_portfolio[asset_type].append(asset_to_include)
            return Portfolio(stock_position = new_portfolio['Stock'], option_position = new_portfolio['Option'],
                             cash_position = new_portfolio['Cash'], fx_positions = new_portfolio['FX'],
                             crypto_positions = new_portfolio['Crypto'], index_positions = new_portfolio['Index'])
    def __add__(self, asset_to_include):

        if isinstance(asset_to_include, Stock):
           # print('S')
            return self.return_new_portfolio(asset_to_include, 'Stock')

        elif isinstance(asset_to_include, Option):
            return self.return_new_portfolio(asset_to_include, 'Option')
        elif isinstance(asset_to_include, Portfolio):
            return self.return_new_portfolio(asset_to_include, 'Portfolio')
        elif isinstance(asset_to_include, Forex):
            return self.return_new_portfolio(asset_to_include, 'FX')
        elif isinstance(asset_to_include, Index):
            return self.return_new_portfolio(asset_to_include, 'Index')
        elif isinstance(asset_to_include, Crypto):
            return self.return_new_portfolio(asset_to_include, 'Crypto')
        elif isinstance(asset_to_include, Cash):
            return self.return_new_portfolio(asset_to_include, 'Cash')



a_1   = Portfolio()

a_2   = Stock('TSLY')

print(a_2.asset_time_series.option_data_frame['Calls'].keys())
furthest_date_tsly = a_2.asset_time_series.option_data_frame['Calls']['2024-03-15'][10.0]

a_3 = Option(furthest_date_tsly)

a_4 = a_1 + a_2

a_4 = a_4 + a_3

print(a_4.generate_combined_value())
print(a_3.asset_time_series.plot_time_series())
print(a_2.asset_time_series.plot_time_series())

a_4.generate_portfolio_returns()


#print('----------------')
#print(a_3.portfolio_of_assets)