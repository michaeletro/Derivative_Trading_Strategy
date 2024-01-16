from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series
from Derivative_Trading_Strategy.Financial_Products.Stock import Stock
from Derivative_Trading_Strategy.Financial_Products.Option import Option
class Portfolio(Time_Series):

    def __init__(self, stocks  = [], options = []):

        self.stocks = stocks
        self.options = options

        self.portfolio_of_assets = {"Stock": self.stocks, "Option": self.options}
        self.value = self.generate_combined_value()

    def generate_combined_value(self):
        cumulative_value = 0
        for key, value in self.portfolio_of_assets.items():
            for items in value:
                cumulative_value = items.value
        return 0


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

new_p = Portfolio(stocks = [Stock('TSLY')], options = [])
