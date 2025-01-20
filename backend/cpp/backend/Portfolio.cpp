#include "Portfolio.h"
#include "custom_hash.h"
#include <vector>
#include <stdexcept>
#include <algorithm>

void Portfolio::addAsset(const std::shared_ptr<Asset>& asset) {
    assets.push_back(asset);
}

double Portfolio::calculateTotalValue() const {
    double total = 0.0;
    for (const auto& asset : assets) {
        total += asset->calculateMovingAverage(1); // Example: latest price
    }
    return total;
}

std::vector<double> Portfolio::calculatePortfolioReturns() const {
    if (assets.empty()) throw std::runtime_error("Portfolio is empty");

    size_t minSize = assets[0]->toJSON()["priceData"].size();
    for (const auto& asset : assets) {
        minSize = std::min(minSize, asset->toJSON()["priceData"].size());
    }

    std::vector<double> returns(minSize, 0.0);
    for (size_t i = 0; i < minSize; ++i) {
        double portfolioValue = 0.0;
        for (const auto& asset : assets) {
            portfolioValue += asset->toJSON()["priceData"][i].get<double>();
        }
        returns[i] = portfolioValue;
    }
    return returns;
}

nlohmann::json Portfolio::toJSON() const {
    nlohmann::json portfolioJSON;
    for (const auto& asset : assets) {
        portfolioJSON["assets"].push_back(asset->toJSON());
    }
    return portfolioJSON;
}