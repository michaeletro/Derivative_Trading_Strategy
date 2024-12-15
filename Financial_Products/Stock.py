from Asset import Asset_Class

class Stock_Class(Asset_Class):

    def __init__(self, ticker):
        print('Generating a Stock Class')
        self.asset_type = Asset_Class(ticker)
        self.asset_name = ticker
        self.asset_label = self.asset_type.asset_label

        self.price_data_frame = self.asset_type.price_data_frame

        # This needs to be sorted
        self.call_options = self.asset_type.call_options
        self.put_options = self.asset_type.put_options
        self.price_vector = self.asset_type.price_vector
        self.at_the_money = int(self.price_vector['Volume_Weighted'].iloc[-1])

    def get_option_by_index(self, option_type='call', index_of_date=0, option_strike = None):
        if option_type.lower() == "call":
                farthest_date_column = self.call_options.columns[index_of_date]
                option_column = self.call_options[farthest_date_column].sort_index()

        elif option_type.lower() == "put":
                farthest_date_column = self.call_options.columns[index_of_date]
                option_column = self.put_options[farthest_date_column].sort_index()

        else:
            return NameError

        if option_strike == None:
            option_strike = self.at_the_money
