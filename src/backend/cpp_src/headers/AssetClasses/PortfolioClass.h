#ifndef PORTFOLIOCLASS_H
#define PORTFOLIOCLASS_H

#include <unordered_map>
#include <memory>
#include "TimeSeriesClass.h"
#include "StockClass.h"
#include "OptionClass.h"
#include "CryptoClass.h"
#include "ForexClass.h"

class Portfolio {
private:
    std::unordered_map<std::string, double> asset_weights;  // Stores asset tickers and their weights

    TimeSeries<std::shared_ptr<Stock>> stock_series;
    TimeSeries<std::shared_ptr<Option>> option_series;
    TimeSeries<std::shared_ptr<Crypto>> crypto_series;
    TimeSeries<std::shared_ptr<Forex>> forex_series;

public:
    Portfolio() = default;

    // ✅ Add entire TimeSeries instead of individual assets
    void addStockSeries(const TimeSeries<std::shared_ptr<Stock>>& series, double weight);
    void addOptionSeries(const TimeSeries<std::shared_ptr<Option>>& series, double weight);
    void addCryptoSeries(const TimeSeries<std::shared_ptr<Crypto>>& series, double weight);
    void addForexSeries(const TimeSeries<std::shared_ptr<Forex>>& series, double weight);

    // double computeExpectedReturn() const;
    // double computeVolatility() const;
    // void optimizePortfolio();
    // void displayPortfolio() const;
};

#endif // PORTFOLIOCLASS_H
