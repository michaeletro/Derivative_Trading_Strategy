#ifndef CRYPTO_H
#define CRYPTO_H

#include "AssetClass.h"

class Crypto : public Asset {
public:
    Crypto(std::string t, std::string d, double o, double c, double h, double l, double v)
        : Asset(std::move(t), std::move(d), o, c, h, l, v) {}

    void print() const override;
};

#endif // CRYPTO_H
