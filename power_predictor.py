from enum import Enum
from math import sqrt

import fbprophet
import matplotlib.pylab as plt
import numpy as np
import pandas as pd
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import QuantileTransformer
import statsmodels.api as sm
from statsmodels.tsa.vector_ar.var_model import VAR
from statsmodels.tsa.vector_ar.vecm import coint_johansen

from model_util import facebook_prophet_filter, lstm_model, Callbacks
from utility import normalize, series_to_supervised, \
    explore_data, calculate_errors


class Constants(Enum):
    TRAIN_TEST_SPLIT_RATIO = 0.8
    CUTOFF_DATE = pd.to_datetime('2013-12-01') # to trim data
    FORECASTED_TEMPERATURE_FILE = 'data/temp_interpolated_load_temperature_data.pickle' # to save/load interpolated result
    DEFAULT_FUTURE_PERIODS = 4 * 24 * 10  # with freq = 15 * 60  that is  1 day
    DEFAULT_FUTURE_FREQ = '15T'  # frequency of recording power, 15 minutes
    # define model configuration
    SARIMAX_ORDER = (7, 1, 7)
    SARIMAX_SEASONAL_ORDER = (0, 0, 0, 0, 12)
    # the following is for lstm model
    WINDOW_TIME_STEPS = 4
    FEATURE_SIZE = 3
    EPOCHS = 5
    INITIAL_EPOCH = 0
    BATCH_SIZE = 72
    MODEL_NAME = 'lstm'


class ColumnNames(Enum):
    POWER = 'actual_kwh'
    TEMPERATURE = 'actual_temperature'
    LABEL = 'y'  # FbProphet & VAR use this name and it should be numeric
    FORECAST = 'yhat'  # FbProphet & VAR use this name and it should be numeric
    DATE = 'date'
    TIME = 'time'
    DATE_STAMP = 'ds'  # Facebook Prophet requires this name
    FEATURES = [LABEL, TEMPERATURE]
    LABELS = [LABEL]
    ORIGINAL_FEATURES = [POWER, TEMPERATURE]


class Models(Enum):
    PROPHET = fbprophet.Prophet(changepoint_prior_scale=0.10, yearly_seasonality=True)
    LSTM = lstm_model(10, (Constants.WINDOW_TIME_STEPS.value, Constants.FEATURE_SIZE.value))
    ARIMA = sm.tsa.statespace.SARIMAX
    VAR = VAR


class PowerForecaster:
    """
    Check out the class spec at
    https://docs.google.com/document/d/1-ceuHfJ2bNbgmKddLTUCS0HJ1juE5t0042Mts_yEUD8v
    sample data is in
    https://drive.google.com/uc?export=download&id=1z2MBYJ8k4M5J3udlFVc2d8opE_f-S4BK
    """

    def __init__(self, df, model=Models.PROPHET, train_test_split_ratio=Constants.TRAIN_TEST_SPLIT_RATIO.value):
        # explore_data(df)
        # first step is to create a timestamp column as index to turn it to a TimeSeries data
        df.index = pd.to_datetime(df[ColumnNames.DATE.value] + df[ColumnNames.TIME.value],
                                  format='%Y-%m-%d%H:%M:%S', errors='raise')

        # keep a copy of original dataset for future comparison
        self.df_original = df.copy()

        # we interpolate temperature using prophet to use it in a multivariate forecast
        temperature = ColumnNames.TEMPERATURE.value
        interpolated_df = facebook_prophet_filter(df, temperature,
                                                  Constants.FORECASTED_TEMPERATURE_FILE.value)
        interpolated_df.index = df.index
        df[[temperature]] = interpolated_df[[ColumnNames.FORECAST.value]]

        # now turn to kwh and make the format compatible with prophet
        df = df.rename(columns={ColumnNames.POWER.value: ColumnNames.LABEL.value})

        # for any regression or forecasting it is better to work with normalized data
        self.transformer = QuantileTransformer()  # handle outliers better than MinMaxScalar
        normalized = normalize(df, ColumnNames.FEATURES.value, transformer=self.transformer)

        # we use the last part (after 12/1/2013) that doesnt have temperature for testing
        cutoff_date = Constants.CUTOFF_DATE.value
        self.df = normalized[normalized.index < cutoff_date]
        self.testing = normalized[normalized.index >= cutoff_date]

        self.df[ColumnNames.DATE_STAMP.value] = self.df.index
        self.train_test_split_ratio = train_test_split_ratio
        self.model = model
        self.train_X, self.test_X = self.train_test_split(self.df[ColumnNames.FEATURES.value])
        self.train_y, self.test_y = self.train_test_split(self.df[ColumnNames.LABELS.value])
        self.model_fit = None
        self.history = None

        # if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
        explore_data(self.df)

    def train_test_split(self, df):
        split_index = int(self.train_test_split_ratio * df.shape[0])
        train = df.iloc[:split_index, :]
        test = df.iloc[split_index:, :]
        return train, test

    def lstm_preprocess(self, df, freq=None):
        upsampled = df if freq is None else df.resample(freq).sum()[ColumnNames.FEATURES.value]

        # frame as supervised learning
        reframed = series_to_supervised(upsampled[ColumnNames.FEATURES.value],
                                        ColumnNames.FEATURES.value, ColumnNames.LABEL.value, 1, 1)
        print(reframed.head())
        # split into train and test sets
        train, test = self.train_test_split(reframed)

        # split into input and outputs
        _train_X, self.train_y = train.iloc[:, :-1], train.iloc[:, -1]
        _test_X, self.test_y = test.iloc[:, :-1], test.iloc[:, -1]
        # reshape input to be 3D [samples, timesteps, features]
        self.train_X = _train_X.values.reshape(
            (_train_X.shape[0], Constants.WINDOW_TIME_STEPS.value, _train_X.shape[1]))
        self.test_X = _test_X.values.reshape((_test_X.shape[0], Constants.WINDOW_TIME_STEPS.value, _test_X.shape[1]))
        print(self.train_X.shape, self.train_y.shape, self.test_X.shape, self.test_y.shape)

    def stationary_test(self):
        dataset = self.test_y.dropna()
        seasonal_dataset = sm.tsa.seasonal_decompose(dataset, freq=365)
        fig = seasonal_dataset.plot()
        fig.set_figheight(8)
        fig.set_figwidth(15)
        fig.show()

        def p_value(dataset):
            # ADF-test(Original-time-series)
            dataset.dropna()
            p_value = sm.tsa.adfuller(dataset, regression='ct')
            print('p-value:{}'.format(p_value))
            p_value = sm.tsa.adfuller(dataset, regression='c')
            print('p-value:{}'.format(p_value))

        p_value(self.train_y)
        p_value(self.test_y)

        # Test works for only 12 variables, check the eigenvalues
        johnsen_test = coint_johansen(self.df[ColumnNames.FEATURES.value].dropna(), -1, 1).eig
        return johnsen_test

    def seasonal_prediction(self):
        from statsmodels.tsa.api import ExponentialSmoothing, SimpleExpSmoothing, Holt
        y_hat_avg = self.test_y.copy()
        fit2 = SimpleExpSmoothing(np.asarray(self.train_y['Count'])).fit(smoothing_level=0.6, optimized=False)
        y_hat_avg['SES'] = fit2.forecast(len(self.test_y))
        plt.figure(figsize=(16, 8))
        plt.plot(self.train_y['Count'], label='Train')
        plt.plot(self.test_y['Count'], label='Test')
        plt.plot(y_hat_avg['SES'], label='SES')
        plt.legend(loc='best')
        plt.show()

    def visual_inspection(self):
        style = [':', '--', '-']
        pd.plotting.register_matplotlib_converters()
        df = self.df

        self.df_original[ColumnNames.ORIGINAL_FEATURES.value].plot(style=style, title='Original Data')
        plt.show()

        self.df[ColumnNames.FEATURES.value].plot(style=style, title='Normalized Data')
        plt.show()

        sampled = df.resample('M').sum()[ColumnNames.FEATURES.value]
        sampled.plot(style=style, title='Aggregated Monthly')
        plt.show()

        sampled = df.resample('W').sum()[ColumnNames.FEATURES.value]
        sampled.plot(style=style, title='Aggregated Weekly')
        plt.show()

        sampled = df.resample('D').sum()[ColumnNames.FEATURES.value]
        sampled.rolling(30, center=True).sum().plot(style=style, title='Aggregated Daily')
        plt.show()

        by_time = df.groupby(by=df.index.time).mean()[ColumnNames.FEATURES.value]
        ticks = 4 * 60 * 60 * np.arange(6)
        by_time.plot(xticks=ticks, style=style, title='Averaged Hourly')
        plt.show()

        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        def tick(x):
            if x % 24 == 12:
                return days[int(x) // 24]
            else:
                return ""

        #        ax.xaxis.set_major_formatter(NullFormatter())
        #        ax.xaxis.set_minor_formatter(FuncFormatter(tick))
        #        ax.tick_params(which="major", axis="x", length=10, width=1.5)

        by_dow = df.groupby(by=df.dow).mean()[ColumnNames.FEATURES.value]
        ticks = 4 * 60 * 60 * np.arange(6)
#        by_dow.plot(xticks=ticks, style=style, title='Averaged on Days of the Week')
#        plt.show()

    def fit(self):
        if self.model == Models.PROPHET:
            self.prophet_fit()
        elif self.model == Models.ARIMA:
            self.arima_fit()
        elif self.model == Models.VAR:
            self.var_fit()
        elif self.model == Models.LSTM:
            self.lstm_fit_simple()
        else:
            raise ValueError("{} is not defined".format(self.model))

    def prophet_fit(self):
        past = self.train_y.copy()
        past[ColumnNames.DATE_STAMP.value] = self.train_y.index
        self.model.value.fit(past)

    def arima_fit(self):
        model = sm.tsa.statespace.SARIMAX(self.train_y,
                                          order=Constants.SARIMAX_ORDER.value,
                                          seasonal_order=Constants.SARIMAX_SEASONAL_ORDER.value)
        # ,enforce_stationarity=False, enforce_invertibility=False, freq='15T')
        print("SARIMAX fitting ....")
        self.model_fit = model.fit()
        self.model_fit.summary()
        print("SARIMAX forecast", self.model_fit.forecast())

    def var_fit(self):
        print("making VAR model")
        model = VAR(endog=self.train_X[ColumnNames.FEATURES.value].dropna())
        print("VAR fitting ....")
        self.model_fit = model.fit()
        self.model_fit.summary()

    def lstm_fit_simple(self):
        history_object = self.model.value.fit(self.train_X, self.train_y, epochs=Constants.EPOCHS.value,
                                              batch_size=Constants.BATCH_SIZE.value,
                                              validation_data=(self.test_X, self.test_y), verbose=2, shuffle=False)
        if history_object is not None:
            self.history = history_object.history

    def lstm_fit(self):
        epochs = Constants.EPOCHS.value
        batch_size = Constants.BATCH_SIZE.value
        model = Models.LSTM.value
        initial_epoch = Constants.INITIAL_EPOCH.value
        callbacks = Callbacks(Constants.MODEL_NAME.value, batch_size, epochs)

        history = model.fit(
            self.train_X,
            self.train_y,
            epochs=epochs,
            batch_size=batch_size,
            validation_split=0.35,
            verbose=0,
            callbacks=callbacks.getDefaultCallbacks(),
            initial_epoch=initial_epoch,

        )

        print(model.summary())
        plt.plot(np.arange(epochs - initial_epoch), history.history['loss'], label='train')
        plt.plot(np.arange(epochs - initial_epoch), history.history['val_loss'], label='validation')
        plt.legend()

    def predict(self):
        future = Constants.DEFAULT_FUTURE_PERIODS.value
        if self.model == Models.PROPHET:
            self.future = self.model.value.make_future_dataframe(periods=future,
                                                                 freq=Constants.DEFAULT_FUTURE_FREQ.value,
                                                                 include_history=False)

        if self.model == Models.PROPHET:
            predicted = self.model.value.predict(self.future)
            predicted[ColumnNames.LABEL.value] = predicted[ColumnNames.FORECAST.value]
        elif self.model == Models.ARIMA:
            predicted = self.arima_predict(future)
        elif self.model == Models.VAR:
            predicted = self.var_predict(future)
        elif self.model == Models.LSTM:
            predicted = self.model.value.predict(self.test_X)
        else:
            raise ValueError("{} is not defined".format(self.model))
        return predicted

    def arima_predict(self, future):
        end = str(self.train_y.index[-1])
        start = str(self.train_y.index[-future])
        print(start, end)
        predicted = self.model_fit.predict(start=start[:10], end=end[:10], dynamic=True)
        return predicted

    def var_predict(self, future):
        predicted_array = self.model_fit.forecast(self.model_fit.y, future)
        predicted = pd.DataFrame(predicted_array)
        predicted.columns = ColumnNames.FEATURES.value
        predicted.index = self.test_y.index[:len(predicted)]
        return predicted

    def sliding_window(self):
        # Generate the data matrix      
        length0 = self.df.shape[0]
        window_size = Constants.WINDOW_TIME_STEPS
        sliding_window_data = np.zeros((length0 - window_size, window_size))
        sliding_window_label = np.zeros((length0 - window_size, 1))
        label_column = ColumnNames.LABEL.value
        for counter in range(length0 - window_size):
            sliding_window_label[counter, :] = self.df[label_column][counter + window_size]
        feature_column = ColumnNames.TEMPERATURE.value
        for counter in range(length0 - window_size):
            sliding_window_data[counter, :] = self.df[feature_column][
                                              counter: counter + window_size]
        print('Random shuffeling')
        length = sliding_window_data.shape[0]
        print("sliding window length", length)
        idx = np.random.choice(length, length, replace=False)
        
        frac = Constants.TRAIN_TEST_SPLIT_RATIO.value
        #if not self.random:
        idx = np.arange(length)
        self.val_idx = idx[int( * length):]

        shuf_data = sliding_window_data[idx, :]
        shuf_label = sliding_window_label[idx, :]

        self.shuf_data = shuf_data
        self.shuf_label = shuf_label
        self.train = sliding_window_data
        self.label = sliding_window_label

        self.train_X = shuf_data[:int(frac * length), :]
        self.train_y = shuf_label[:int(frac * length), :]
        self.train_size = int(frac * length)

        self.test_X = shuf_data[int(frac * length):, :]
        self.test_y = shuf_label[int(frac * length):, :]
        self.val_size = int((1 - frac) * length)

        return None

    def evaluate(self):
        # make a prediction
        yhat = self.model.value.predict(self.test_X)
        test_X = self.test_X.reshape((self.test_X.shape[0], self.test_X.shape[2]))
        # invert scaling for forecast
        inv_yhat = pd.concatenate((yhat, test_X[:, 1:]), axis=1)
        inv_yhat = self.transformer.inverse_transform(inv_yhat)
        inv_yhat = inv_yhat[:, 0]
        # invert scaling for actual
        test_y = self.test_y.reshape((len(self.test_y), 1))
        inv_y = pd.concatenate((test_y, test_X[:, 1:]), axis=1)
        inv_y = self.transformer.inverse_transform(inv_y)
        inv_y = inv_y[:, 0]
        # calculate RMSE
        rmse = sqrt(mean_squared_error(inv_y, inv_yhat))
        print('Test RMSE: %.3f' % rmse)

    def plot_future(self, predicted):
        self.model.value.plot(predicted, xlabel='Date', ylabel='KWH')
        self.model.value.plot_components(predicted)

    def plot_prediction(self, predicted):
        style = [':', '--', '-']
        pd.plotting.register_matplotlib_converters()
        label_column = ColumnNames.LABELS.value
        plt.plot(predicted.index, self.test_y[label_column].iloc[:len(predicted)], predicted[label_column])
        plt.show()

    def plot_history(self):
        plt.plot(self.history['loss'])
        plt.plot(self.history['val_loss'])
        plt.title('model accuracy')
        plt.ylabel('accuracy')
        plt.xlabel('epoch')
        plt.legend(['train', 'test'], loc='upper left')
        plt.show()


class ModelEvaluator:

    def cross_k_validation(self, model):
        tscv = TimeSeriesSplit(n_splits=10)
        for train_index, test_index in tscv.split(self.df_normalized):
            print("TRAIN:", train_index, "TEST:", test_index)
            y_column = self.df_normalized[ColumnNames.LABEL.value]
            y_train, y_test = y_column[train_index], y_column[test_index]
            model.fit(pd.DataFrame(y_train))
            forecast = model.forecast(None)
            print(y_test.shape)
            print(forecast.shape)
            calculate_errors(y_test, forecast)
            plt.plot(y_test, 'g')
            plt.plot(forecast, 'b')
            size = len(y_test)
            plt.xlim(size - 1000, size)
            plt.show()
