#ifndef OPTION_CLASS_H
#define OPTION_CLASS_H

#include "AssetClass.h"
#include <iostream>

class Option : public Asset {
    public:
        // 🔥 Corrected constructor: Now passes 7 required arguments to Asset
        Option(std::string t, std::string d, double o, double c, double h, double l, double v,
            double iv, double sp, std::string ot, bool ic, std::string expiry)
            : Asset(t, d, o, c, h, l, v),  // 🔥 Fixed constructor call
            implied_volatility(iv), strike_price(sp), option_type(ot), is_call(ic), expiry_date(expiry) {}

        // 🔥 Overloaded constructor for TimeSeries compatibility
        Option(std::string t, std::string d, double o, double c, double h, double l, double v)
            : Asset(t, d, o, c, h, l, v),  // 🔥 Ensure all required arguments are passed
            implied_volatility(0.0), strike_price(0.0), option_type(""), is_call(false), expiry_date("") {}

        // ✅ Calculate Intrinsic Value
        double calculateIntrinsicValue() const;

        // ✅ Calculate Time Value
        double calculateTimeValue(double market_price) const;

        // ✅ Calculate Option Greeks (Delta)
        double calculateDelta(double volatility, double interest_rate, double time_to_expiry) const;

        // ✅ Print Option Details
        void print() const override;

private:

    double implied_volatility, strike_price;
    std::string option_type;
    bool is_call;
    std::string expiry_date;
};

#endif // OPTION_CLASS_H
