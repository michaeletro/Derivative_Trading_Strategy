#ifndef FOREX_H
#define FOREX_H

#include "AssetClass.h"

class Forex : public Asset {
public:
    Forex(std::string t, std::string d, double o, double c, double h, double l, double v)
        : Asset(std::move(t), std::move(d), o, c, h, l, v) {}

    void print() const override;
};

#endif // FOREX_H
