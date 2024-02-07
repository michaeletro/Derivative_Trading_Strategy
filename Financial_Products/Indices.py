from Derivative_Trading_Strategy.Financial_Products.Asset import Asset
class Index(Asset):
    def __int__(self, asset):
        self.asset = asset