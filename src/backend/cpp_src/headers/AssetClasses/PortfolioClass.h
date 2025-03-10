#ifndef PORTFOLIO_H
#define PORTFOLIO_H

#include <unordered_map>
#include <memory>
#include <iostream>
#include <Eigen/Dense>
#include "TimeSeriesClass.h"
#include "AssetClass.h"
#include "StockClass.h"
#include "OptionClass.h"
#include "CryptoClass.h"
#include "ForexClass.h"

class Portfolio {
private:
    std::unordered_map<std::string, double> asset_weights;
    double risk_free_rate = 0.02;

public:
    TimeSeries<std::shared_ptr<Asset>> portfolio_series;  // ✅ Now properly recognized

    Portfolio() = default;

    void addAssetSeries(const TimeSeries<std::shared_ptr<Asset>>& series, double weight);

    double computeExpectedReturn() const;
    double computeVolatility(int rolling_window) const;
    double computeSharpeRatio(int rolling_window) const;
    void optimizePortfolio();
    void loadFromDatabase();
    void displayPortfolio() const;
};

#endif // PORTFOLIO_H
