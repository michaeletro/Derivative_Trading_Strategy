from Derivative_Trading_Strategy.Financial_Products.Time_Series import Time_Series

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


        return result_dict

    def __setattr__(self, value_replacing, value_to_be_replaced):

        if isinstance(value_replacing, Stock):
            print("Setting the Stocks in our portfolio to a new list of stocks. ")
        elif isinstance(value_replacing, Option):
            print("Setting the Options in portfolio to a new list of options.")
        elif isinstance(value_replacing, Portfolio):
            print("Setting the Portfolio to a new Portfolio")
        else:
            print('Incompatible Value')
            return ValueError

        super().__setattr__(value_replacing, value_to_be_replaced)

    def __iter__(self):
        self.current_index = 0
        return self

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