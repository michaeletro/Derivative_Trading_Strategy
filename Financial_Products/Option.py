from .Asset import Asset_Class

class Option_Class(Asset_Class):
    """
    A class to represent and manage option data for a given financial asset.

    This class inherits from the Asset_Class and initializes option data
    for a specified asset name.

    Attributes:
    -----------
    asset_name : Asset_Class
        An instance of the Asset_Class initialized with the asset name.
    asset_label : str
        The label of the asset.
    price_data_frame : DataFrame
        The price data frame of the asset.
    at_the_money : int
        The at-the-money strike price of the asset.
    price_vector : DataFrame
        The price vector data frame of the asset.

    Methods:
    --------
    __init__(self, asset_name):
        Initializes the Option_Class with the specified asset name and creates an Asset_Class instance.
    """
    def __init__(self, ticker):
        print('Generating an Option Class')
        super().__init__(ticker)
        self._generate_price_data()

