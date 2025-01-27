#ifndef PORTFOLIOCLASS_H
#define PORTFOLIOCLASS_H

#include "AssetClass.h"
#include <map>
#include <memory>
#include <string>
#include <vector>

class PortfolioClass {
public:
    PortfolioClass() = default;
    ~PortfolioClass() = default;

    void addToPortfolio(const std::shared_ptr<AssetClass>& asset, const std::string& assetType);
    void subtractFromPortfolio(const std::shared_ptr<AssetClass>& asset, const std::string& assetType);
    void addToPortfolio(const PortfolioClass& portfolio);
    void subtractFromPortfolio(const PortfolioClass& portfolio);

    void printPortfolio() const;

private:
    std::map<std::string, std::vector<std::shared_ptr<AssetClass>>> portfolio_of_assets;
};

#endif // PORTFOLIOCLASS_H
