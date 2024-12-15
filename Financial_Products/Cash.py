from Asset import Asset_Class

class Cash_Class(Asset_Class):

    def __init__(self, cash_amount):
        print('Generating a Cash Class')

        self.cash_amount = cash_amount


    def __add__(self, new_financing):
        self.cash_amount = self.cash_amount + new_financing

    def __sub__(self, new_financing):
        self.cash_amount = self.cash_amount - new_financing

    def __eq__(self, new_financing):
        if isinstance(Cash, new_financing) and new_financing.cash_amount == self.cash_amount:
            return True
        else:
            return False
