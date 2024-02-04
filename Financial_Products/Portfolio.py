from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series
from Derivative_Trading_Strategy.Financial_Products.Stock import Stock
from Derivative_Trading_Strategy.Financial_Products.Option import Option
from Derivative_Trading_Strategy.Financial_Products.Asset import Asset

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

        cumulative_value = 0
        for key, value in self.portfolio_of_assets.items():
            for items in value:
                cumulative_value = items.price_vector + cumulative_value
        return cumulative_value


    def __add__(self, asset_to_include):

        if isinstance(asset_to_include, Stock):
            new_portfolio = {'Stock': self.portfolio_of_assets['Stock'].append(asset_to_include),
                             'Option': self.portfolio_of_assets['Option']}
            return Portfolio(stock_position = new_portfolio['Stock'], option_position = new_portfolio['Option'])

        elif isinstance(asset_to_include, Option):
            new_portfolio = {'Stock': self.portfolio_of_assets['Stock'],
                             'Option': self.portfolio_of_assets['Option'].append(asset_to_include)}
            return Portfolio(stock_position = new_portfolio['Stock'], option_position = new_portfolio['Option'])
        elif isinstance(asset_to_include, Portfolio):
            new_portfolio = {'Stock':  self.portfolio_of_assets['Stock'].append(asset_to_include.portfolio_of_assets['Stock']),
                             'Option': self.portfolio_of_assets['Option'].append(asset_to_include.portfolio_of_assets['Option'])}
            return Portfolio(stock_position = new_portfolio['Stock'], option_position = new_portfolio['Option'])

