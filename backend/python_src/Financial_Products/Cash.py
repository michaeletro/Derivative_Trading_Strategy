from .Asset import Asset_Class


class Cash_Class(Asset_Class):
    """
    This module defines the `Cash_Class` for managing cash data in the financial products package.

    Classes:
    --------
    Cash_Class(Asset_Class):
        A class to represent and manage cash data for a given financial asset.

        Methods:
        --------
        __init__(self, cash_amount):
            Initializes the Cash_Class with the specified cash amount and creates an Asset_Class instance.

        __add__(self, new_financing):
            Adds the new financing amount to the current cash amount.

        __sub__(self, new_financing):
            Subtracts the new financing amount from the current cash amount.

        __eq__(self, new_financing):
            Checks if the new financing amount is equal to the current cash amount.
    """
    def __init__(self, cash_amount):
        print('Generating a Cash Class')

        self.cash_amount = cash_amount


    def __add__(self, new_financing):
        self.cash_amount = self.cash_amount + new_financing

    def __sub__(self, new_financing):
        self.cash_amount = self.cash_amount - new_financing

    def __eq__(self, new_financing):
        if isinstance(Cash_Class, new_financing) and new_financing.cash_amount == self.cash_amount:
            return True
        else:
            return False
