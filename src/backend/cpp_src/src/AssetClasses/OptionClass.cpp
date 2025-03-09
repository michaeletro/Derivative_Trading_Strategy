#include "../../headers/Assets/Option.h"
#include <iostream>
#include <cmath>

// ✅ Calculate Intrinsic Value
double Option::calculateIntrinsicValue() const {
    double intrinsic = is_call ? std::max(0.0, close_price - strike_price) 
                               : std::max(0.0, strike_price - close_price);
    return intrinsic;
}

// ✅ Calculate Time Value
double Option::calculateTimeValue(double market_price) const {
    return market_price - calculateIntrinsicValue();
}

// ✅ Calculate Delta (Approximation using Black-Scholes)
double Option::calculateDelta(double volatility, double interest_rate, double time_to_expiry) const {
    double d1 = (log(close_price / strike_price) + (interest_rate + 0.5 * volatility * volatility) * time_to_expiry) /
                (volatility * sqrt(time_to_expiry));
    
    return is_call ? 0.5 * (1 + erf(d1 / sqrt(2)))  // Call Delta
                   : -0.5 * (1 + erf(-d1 / sqrt(2))); // Put Delta
}

// ✅ Print Option Info
void Option::print() const {
    std::cout << "📜 Option (" << (is_call ? "Call" : "Put") << "): ";
    Asset::print();
    std::cout << " | Strike: " << strike_price << " | Expiry: " << expiry_date 
              << " | Intrinsic: " << calculateIntrinsicValue() << "\n";
}
