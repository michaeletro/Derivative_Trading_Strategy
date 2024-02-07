from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Forex(Asset):
    def __init__(self, currency_swap_position):
        self.asset_time_series = Asset(currency_swap_position)
        try:
            self.price_data_frame = self.asset_time_series.price_data_frame
            self.price_vector = self.asset_time_series.price_vector
        except TypeError:
            self.price_vector = []
            self.price_vector = []