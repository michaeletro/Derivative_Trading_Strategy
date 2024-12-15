import plotly.express as px


class Portfolio_Class:

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


    def return_new_portfolio(self, asset_to_include, asset_type):
        new_portfolio = self.portfolio_of_assets

        if isinstance(asset_to_include, Portfolio):
            for key, value in asset_to_include.portfolio_of_assets.items():
                if len(value) == 0:
                    continue
                else:
                    for i in range(0,len(value)-1):
                        print(i)
                        new_portfolio[key].append(value[i])
            print(len(new_portfolio['Stock']))
            return Portfolio(stock_position=new_portfolio['Stock'], option_position=new_portfolio['Option'],
                             cash_position=new_portfolio['Cash'], fx_positions=new_portfolio['FX'],
                             crypto_positions=new_portfolio['Crypto'], index_positions=new_portfolio['Index'])
        else:
            new_portfolio[asset_type].append(asset_to_include)
            print(len(new_portfolio['Stock']))
            return Portfolio(stock_position = new_portfolio['Stock'], option_position = new_portfolio['Option'],
                             cash_position = new_portfolio['Cash'], fx_positions = new_portfolio['FX'],
                             crypto_positions = new_portfolio['Crypto'], index_positions = new_portfolio['Index'])
    def __add__(self, asset_to_include):

        if isinstance(asset_to_include, Stock):
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

        """  def __del__(self):
            self.stock_position = []
            self.option_position = []
            self.cash_position = []
            self.fx_positions = []
            self.crypto_position = []
            self.index_positions = []
            self.portfolio_of_assets = {"Stock": self.stock_position, "Option": self.option_position,
                                        "Cash": self.cash_position, "FX": self.fx_positions,
                                        "Crypto": self.crypto_position, "Index": self.index_positions}
    
            del self.stock_position
            del self.option_position
            del self.cash_position
            del self.fx_positions
            del self.crypto_position
            del self.index_positions
            del self.portfolio_of_assets
        """
