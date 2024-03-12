class Index_Error(Exception):
    def __init__(self):
        pass
    def __str__(self):
        print('There was an error with your index')

class Date_Error(Index_Error):
    def __init__(self, error_date):
        self.error_date = error_date
    def __str__(self):
        print('There was an error with your date')