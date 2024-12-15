
class Index_Error(Exception):
    """
    This module defines custom error types for handling specific exceptions in the financial products package.

    Classes:
    --------
    Index_Error(Exception):
        A custom exception class for handling index-related errors.

        Methods:
        --------
        __init__(self):
            Initializes the Index_Error class.

        __str__(self):
            Returns a string representation of the index error.

    Date_Error(Index_Error):
        A custom exception class for handling date-related errors, inheriting from Index_Error.

        Methods:
        --------
        __init__(self, error_date):
            Initializes the Date_Error class with the specified error date.

        __str__(self):
            Returns a string representation of the date error.
    """
    def __init__(self):
        pass
    def __str__(self):
        print('There was an error with your index')

class Date_Error(Index_Error):
    def __init__(self, error_date):
        self.error_date = error_date
    def __str__(self):
        print('There was an error with your date')