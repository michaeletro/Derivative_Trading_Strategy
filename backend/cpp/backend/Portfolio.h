#ifndef PORTFOLIO_H
#define PORTFOLIO_H

#include <vector>
#include <memory>
#include "../libs/json/single_include/nlohmann/json.hpp"
#include "Asset.h"

class Portfolio {
public:
    void addAsset(const std::shared_ptr<Asset>& asset);
    double calculateTotalValue() const;
    std::vector<double> calculatePortfolioReturns() const;
    nlohmann::json toJSON() const;

private:
    std::vector<std::shared_ptr<Asset>> assets;
};

#endif // PORTFOLIO_H