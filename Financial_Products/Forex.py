from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Forex(Asset):
    def __init__(self, currency_swap_position):
        self.Forex_Position = currency_swap_position