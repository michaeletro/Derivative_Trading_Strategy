#include "../../headers/AssetClasses/PortfolioClass.h"
#include <iostream>
#include <Eigen/Dense>

// ✅ Add entire time series to portfolio
void Portfolio::addStockSeries(const TimeSeries<std::shared_ptr<Stock>>& series, double weight) {
    stock_series = series;
    for (const auto& asset : series.getData()) {
        asset_weights[asset->getTicker()] = weight;
    }
}

void Portfolio::addOptionSeries(const TimeSeries<std::shared_ptr<Option>>& series, double weight) {
    option_series = series;
    for (const auto& asset : series.getData()) {
        asset_weights[asset->getTicker()] = weight;
    }
}

void Portfolio::addCryptoSeries(const TimeSeries<std::shared_ptr<Crypto>>& series, double weight) {
    crypto_series = series;
    for (const auto& asset : series.getData()) {
        asset_weights[asset->getTicker()] = weight;
    }
}

void Portfolio::addForexSeries(const TimeSeries<std::shared_ptr<Forex>>& series, double weight) {
    forex_series = series;
    for (const auto& asset : series.getData()) {
        asset_weights[asset->getTicker()] = weight;
    }
}

/*
// ✅ Compute expected return using all time series
double Portfolio::computeExpectedReturn() const {
    double total_return = 0.0;

    auto compute_return = [&](const auto& series) {
        for (const auto& asset : series.getData()) {
            total_return += asset_weights.at(asset->getTicker()) * asset->getExpectedReturn();
        }
    };

    compute_return(stock_series);
    compute_return(option_series);
    compute_return(crypto_series);
    compute_return(forex_series);

    return total_return;
}

// ✅ Compute volatility using covariance across time series
double Portfolio::computeVolatility() const {
    int num_assets = stock_series.getSize() + option_series.getSize() + crypto_series.getSize() + forex_series.getSize();
    
    Eigen::MatrixXd cov_matrix(num_assets, num_assets);
    Eigen::VectorXd weights(num_assets);
    int index = 0;

    auto fill_matrices = [&](const auto& series) {
        for (int i = 0; i < series.getSize(); i++) {
            auto asset_i = series.getAsset(i);
            weights(index) = asset_weights.at(asset_i->getTicker());

            for (int j = 0; j < series.getSize(); j++) {
                auto asset_j = series.getAsset(j);
                cov_matrix(index, j) = asset_i->computeCovariance(*asset_j);
            }
            index++;
        }
    };

    fill_matrices(stock_series);
    fill_matrices(option_series);
    fill_matrices(crypto_series);
    fill_matrices(forex_series);

    double portfolio_variance = weights.transpose() * cov_matrix * weights;
    return std::sqrt(portfolio_variance);
}

// ✅ Optimize portfolio weights using time series data
void Portfolio::optimizePortfolio() {
    int num_assets = stock_series.getSize() + option_series.getSize() + crypto_series.getSize() + forex_series.getSize();

    Eigen::VectorXd expected_returns(num_assets);
    Eigen::MatrixXd cov_matrix(num_assets, num_assets);
    int index = 0;

    auto fill_matrices = [&](const auto& series) {
        for (int i = 0; i < series.getSize(); i++) {
            auto asset_i = series.getAsset(i);
            expected_returns(index) = asset_i->getExpectedReturn();

            for (int j = 0; j < series.getSize(); j++) {
                auto asset_j = series.getAsset(j);
                cov_matrix(index, j) = asset_i->computeCovariance(*asset_j);
            }
            index++;
        }
    };

    fill_matrices(stock_series);
    fill_matrices(option_series);
    fill_matrices(crypto_series);
    fill_matrices(forex_series);

    // Solve for optimal weights using quadratic programming
    Eigen::VectorXd optimal_weights = cov_matrix.inverse() * expected_returns;
    optimal_weights /= optimal_weights.sum(); // Normalize weights to sum to 1

    index = 0;
    auto assign_weights = [&](const auto& series) {
        for (int i = 0; i < series.getSize(); i++) {
            auto asset = series.getAsset(i);
            asset_weights[asset->getTicker()] = optimal_weights(index);
            index++;
        }
    };

    assign_weights(stock_series);
    assign_weights(option_series);
    assign_weights(crypto_series);
    assign_weights(forex_series);
}

// ✅ Display portfolio asset weights and expected return
void Portfolio::displayPortfolio() const {
    std::cout << "Portfolio Composition:\n";

    auto print_series = [&](const auto& series) {
        for (const auto& asset : series.getData()) {
            std::cout << asset->getTicker() << ": " << (asset_weights.at(asset->getTicker()) * 100)
                      << "% | Expected Return: " << asset->getExpectedReturn() << "\n";
        }
    };

    print_series(stock_series);
    print_series(option_series);
    print_series(crypto_series);
    print_series(forex_series);

    std::cout << "Portfolio Expected Return: " << computeExpectedReturn() << "\n";
    std::cout << "Portfolio Volatility: " << computeVolatility() << "\n";
}
*/