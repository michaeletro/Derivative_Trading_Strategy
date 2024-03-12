from Derivative_Trading_Strategy.Financial_Products.Time_Series import *
class Asset(Time_Series):

    def __init__(self, asset_name):
        print('Generating an Asset Class')
        self.asset_label = asset_name
        self.asset_name = asset_name

        if len(asset_name) > 5:
            self.asset_name = f'O:{asset_name}'
        else:
            self.option_data_frame = self.generate_option_ticker()
            self.call_options = self.option_data_frame['Calls']
            self.put_options = self.option_data_frame['Puts']


        self.asset_time_series = Time_Series(asset_name)
        self.price_data_frame = self.generate_asset_info()

        try:
            self.price_vector = self.price_data_frame[['Time','Volume_Weighted']]
        except TypeError as TE:
            self.price_vector = []
            print(f'Type Error of {TE}')

        self.current_price = self.price_data_frame.iloc[len(self.price_data_frame)-1]

    def generate_asset_info(self):
        response = self.asset_time_series.api_object.generate_request()
        try:
            for i in range(0, len(response['results'])):
                response['results'][i]['t'] = pd.to_datetime(response['results'][i]['t'], unit='ms')

                response['results'][i]['Time'] = response['results'][i].pop('t')
                response['results'][i]['Volume_Weighted'] = response['results'][i].pop('vw')
                response['results'][i]['Open_Price'] = response['results'][i].pop('o')
                response['results'][i]['Lowest_Price'] = response['results'][i].pop('l')
                response['results'][i]['Highest_Price'] = response['results'][i].pop('h')
                response['results'][i]['Close_Price'] = response['results'][i].pop('c')
                response['results'][i]['Volume'] = response['results'][i].pop('v')
                response['results'][i]['Lot_Size'] = response['results'][i].pop('n')

            self.organized_data = pd.DataFrame(response['results'])
            return self.organized_data
        except:
            return f'There was an error with the API request of type'
    def generate_option_ticker(self):
        try:
            ticker_list = yf.Ticker(self.asset_name)
            master_dict = {"Calls": {}, "Puts": {}}
            for i in range(0, len(ticker_list.options)):
                master_dict["Calls"][ticker_list.options[i]] = {}
                master_dict["Puts"][ticker_list.options[i]] = {}
            for j in master_dict["Calls"].keys():

                call_df_for_date = ticker_list.option_chain(date=j).calls
                puts_df_for_date = ticker_list.option_chain(date=j).puts

                for k in range(0, len(call_df_for_date['strike'])):
                    master_dict["Calls"][j][call_df_for_date['strike'][k]] = call_df_for_date['contractSymbol'][k]
                for k in range(0, len(puts_df_for_date['strike'])):
                    master_dict["Puts"][j][puts_df_for_date['strike'][k]] = puts_df_for_date['contractSymbol'][k]

            return {'Calls': pd.DataFrame(master_dict['Calls']), 'Puts': pd.DataFrame(master_dict['Puts'])}
        except TypeError as e:
            print(f'Error on retrieving option ticker. Error type {e}')
            return {'Calls': [], 'Puts': []}
    def plot_time_series(self, start_date = datetime(2023,1,1), end_date = datetime.today()):

        fig = px.line(self.price_data_frame, x = 'Time', y = 'Volume_Weighted',
                      title = self.asset_label)
        fig.show()

    def __del__(self):
        print('Destructor called, object deleted')

