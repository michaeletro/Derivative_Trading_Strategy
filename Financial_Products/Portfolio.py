from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series
from Derivative_Trading_Strategy.Financial_Products.Stock import Stock
from Derivative_Trading_Strategy.Financial_Products.Option import Option
from Derivative_Trading_Strategy.Financial_Products.Asset import Asset

class Portfolio(Time_Series):

    def __init__(self, stocks  = [], options = []):
        print('Generating a Portfolio Class')
        self.stocks = stocks
        self.options = options

        self.portfolio_of_assets = {"Stock": self.stocks, "Option": self.options}
        #self.value = self.generate_combined_value()

    def generate_combined_value(self):
        cumulative_value = 0
        for key, value in self.portfolio_of_assets.items():
            for items in value:
                print(items.price_vector)
                cumulative_value = items.price_vector + cumulative_value
        return cumulative_value


    def __add__(self, asset_to_include):

        if isinstance(asset_to_include, Stock):
            new_portfolio = {'Stock': self.portfolio_of_assets['Stock'].append(asset_to_include),
                             'Option': self.portfolio_of_assets['Option']}
            return Portfolio(stocks = new_portfolio['Stock'], options = new_portfolio['Option'])

        elif isinstance(asset_to_include, Option):
            new_portfolio = {'Stock': self.portfolio_of_assets['Stock'],
                             'Option': self.portfolio_of_assets['Option'].append(asset_to_include)}
            return Portfolio(stocks = new_portfolio['Stock'], options = new_portfolio['Option'])
        elif isinstance(asset_to_include, Portfolio):
            new_portfolio = {'Stock':  self.portfolio_of_assets['Stock'].append(asset_to_include.portfolio_of_assets['Stock']),
                             'Option': self.portfolio_of_assets['Option'].append(asset_to_include.portfolio_of_assets['Option'])}
            return Portfolio(stocks = new_portfolio['Stock'], options = new_portfolio['Option'])

