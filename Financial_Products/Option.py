from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Option(Asset):

    def __init__(self, asset_name):
        print('Generating an Option Class')

        self.asset_name = Asset(asset_name)
        self.asset_label = self.asset_name.asset_label
        self.price_data_frame = self.asset_name.price_data_frame
        self.at_the_money = int(self.asset_name.current_price['Volume_Weighted'])
        self.price_vector = self.asset_name.price_vector
