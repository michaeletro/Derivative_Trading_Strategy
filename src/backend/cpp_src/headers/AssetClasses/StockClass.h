#ifndef STOCK_H
#define STOCK_H

#include "AssetClass.h"

class Stock : public Asset {
private:
    double dividend_yield = 0.0;

public:
    Stock(std::string t, std::string d, double o, double c, double h, double l, double v)
        : Asset(std::move(t), std::move(d), o, c, h, l, v) {}

    void setDividendYield(double yield) { dividend_yield = yield; }
    double getDividendYield() const { return dividend_yield; }

    double calculateDividendPayout(double shares_owned) const {
        return (dividend_yield / 100.0) * shares_owned * close_price;
    }

    void print() const override;
};

#endif // STOCK_H
