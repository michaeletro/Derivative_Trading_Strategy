#include "../../headers/AssetClasses/PortfolioClass.h"
#include <random>
#include <algorithm>

// ✅ Add Asset Series to Portfolio
void Portfolio::addAssetSeries(const TimeSeries<std::shared_ptr<Asset>>& series, double weight) {
    for (const auto& asset : series.getData()) {
        portfolio_series.addAsset(asset);
        asset_weights[asset->getTicker()] = weight;
    }
}
// ✅ Compute Portfolio Expected Return from Time-Series
double Portfolio::computeExpectedReturn() const {
    double total_return = 0.0;
    for (const auto& asset : portfolio_series.getData()) {
        total_return += asset_weights.at(asset->getTicker()) * asset->getExpectedReturn();
    }
    return total_return;
}

// ✅ Compute Rolling Portfolio Volatility
double Portfolio::computeVolatility(int rolling_window) const {
    int n = portfolio_series.size();
    if (n < rolling_window) return 0.0;

    Eigen::MatrixXd cov_matrix(n, n);
    Eigen::VectorXd weights(n);

    for (int i = 0; i < n; ++i) {
        weights(i) = asset_weights.at(portfolio_series[i]->getTicker());
        for (int j = 0; j < n; ++j) {
            cov_matrix(i, j) = portfolio_series[i]->computeCovariance(*portfolio_series[j]);
        }
    }

    return std::sqrt(weights.transpose() * cov_matrix * weights);
}

// ✅ Compute Sharpe Ratio with Rolling Volatility
double Portfolio::computeSharpeRatio(int rolling_window) const {
    double expected_return = computeExpectedReturn();
    double volatility = computeVolatility(rolling_window);
    return (expected_return - risk_free_rate) / volatility;
}

// ✅ Optimize Portfolio Weights using Markowitz MPT
void Portfolio::optimizePortfolio() {
    int n = portfolio_series.size();
    Eigen::MatrixXd cov_matrix(n, n);
    Eigen::VectorXd returns(n);
    Eigen::VectorXd weights(n);

    for (int i = 0; i < n; ++i) {
        returns(i) = portfolio_series[i]->getExpectedReturn();
        weights(i) = asset_weights.at(portfolio_series[i]->getTicker());
        for (int j = 0; j < n; ++j) {
            cov_matrix(i, j) = portfolio_series[i]->computeCovariance(*portfolio_series[j]);
        }
    }

    Eigen::VectorXd optimal_weights = cov_matrix.inverse() * returns;
    optimal_weights /= optimal_weights.sum(); // Normalize weights

    for (int i = 0; i < n; ++i) {
        asset_weights[portfolio_series[i]->getTicker()] = optimal_weights(i);
    }
}

// ✅ Load Portfolio Time-Series from Database
void Portfolio::loadFromDatabase() {
    std::cout << "🔄 Loading portfolio data from database..." << std::endl;
    // Load historical time-series of assets
}

// ✅ Display Portfolio Summary
void Portfolio::displayPortfolio() const {
    std::cout << "📊 Portfolio Summary:\n";
    for (const auto& asset : portfolio_series.getData()) {
        std::cout << "✅ " << asset->getTicker() << " | Weight: " << asset_weights.at(asset->getTicker()) * 100 
                  << "% | Expected Return: " << asset->getExpectedReturn() << "\n";
    }
    std::cout << "⚡ Sharpe Ratio: " << computeSharpeRatio(30) << "\n";
}
