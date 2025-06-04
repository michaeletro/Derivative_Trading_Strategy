from .Asset import Asset_Class

class Crypto_Class(Asset_Class):
    """
    This module defines the `Crypto_Class` for managing cryptocurrency data in the financial products package.

    Classes:
    --------
    Crypto_Class(Asset_Class):
        A class to represent and manage cryptocurrency data for a given financial asset.

        Methods:
        --------
        __init__(self, asset):
            Initializes the Crypto_Class with the specified asset and creates an Asset_Class instance.
    """
    def __init__(self, asset_name):
        """Initialize the Crypto_Class with the given asset name."""
        print('Generating a Crypto Class')
        super().__init__(asset_name=asset_name)
        self.pricing_data = self.generate_price_data()
