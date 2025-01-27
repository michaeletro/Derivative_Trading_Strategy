#include "PortfolioClass.h"
#include <algorithm>
#include <stdexcept>
#include <iostream>

// Add asset to portfolio
void PortfolioClass::addToPortfolio(const std::shared_ptr<AssetClass>& asset, const std::string& assetType) {
    portfolio_of_assets[assetType].push_back(asset);
}

// Remove asset from portfolio
void PortfolioClass::subtractFromPortfolio(const std::shared_ptr<AssetClass>& asset, const std::string& assetType) {
    auto& assets = portfolio_of_assets[assetType];
    auto it = std::find_if(assets.begin(), assets.end(), [&](const std::shared_ptr<AssetClass>& a) {
        return *a == *asset; // Compare dereferenced objects
    });

    if (it != assets.end()) {
        assets.erase(it);
    } else {
        throw std::runtime_error("Asset not found in portfolio.");
    }
}

// Add portfolio to current portfolio
void PortfolioClass::addToPortfolio(const PortfolioClass& portfolio) {
    for (const auto& [assetType, assets] : portfolio.portfolio_of_assets) {
        portfolio_of_assets[assetType].insert(portfolio_of_assets[assetType].end(), assets.begin(), assets.end());
    }
}

// Remove portfolio from current portfolio
void PortfolioClass::subtractFromPortfolio(const PortfolioClass& portfolio) {
    for (const auto& [assetType, assetsToRemove] : portfolio.portfolio_of_assets) {
        auto& currentAssets = portfolio_of_assets[assetType];

        for (const auto& asset : assetsToRemove) {
            auto it = std::find_if(currentAssets.begin(), currentAssets.end(), [&](const std::shared_ptr<AssetClass>& a) {
                return *a == *asset; // Compare dereferenced objects
            });

            if (it != currentAssets.end()) {
                currentAssets.erase(it);
            }
        }
    }
}

// Print portfolio details
void PortfolioClass::printPortfolio() const {
    for (const auto& [assetType, assets] : portfolio_of_assets) {
        std::cout << "Asset Type: " << assetType << "\n";
        for (const auto& asset : assets) {
            std::cout << "  - " << asset->getAssetName() << "\n";
        }
    }
}
