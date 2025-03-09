#ifndef OPTION_H
#define OPTION_H

#include "Asset.h"
#include <cmath>

class Option : public Asset {
private:
    double strike_price;
    std::string expiry_date;
    bool is_call;  // true for call, false for put

public:
    Option(std::string t, std::string d, double o, double c, double h, double l, double v, 
           double sp, std::string exp, bool call)
        : Asset(std::move(t), std::move(d), o, c, h, l, v), 
          strike_price(sp), expiry_date(std::move(exp)), is_call(call) {}

    // ✅ Calculate Intrinsic Value
    double calculateIntrinsicValue() const;

    // ✅ Calculate Time Value
    double calculateTimeValue(double market_price) const;

    // ✅ Calculate Option Greeks (Delta)
    double calculateDelta(double volatility, double interest_rate, double time_to_expiry) const;

    // ✅ Print Option Details
    void print() const override;
};

#endif // OPTION_H
