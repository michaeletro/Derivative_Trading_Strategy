#ifndef ASSET_H
#define ASSET_H

#include <string>
#include <iostream>

class Asset {
protected:
    std::string ticker;
    std::string date;
    double open_price, close_price, high_price, low_price, volume;

public:
    Asset(std::string t, std::string d, double o, double c, double h, double l, double v)
        : ticker(std::move(t)), date(std::move(d)), open_price(o), close_price(c), 
          high_price(h), low_price(l), volume(v) {}

    virtual ~Asset() = default;

    virtual void print() const {
        std::cout << "📊 " << ticker << " | " << date
                  << " | Open: " << open_price << " | Close: " << close_price
                  << " | High: " << high_price << " | Low: " << low_price
                  << " | Volume: " << volume << "\n";
    }

    // ✅ Getters
    std::string getTicker() const { return ticker; }
    std::string getDate() const { return date; }
    double getOpenPrice() const { return open_price; }
    double getClosePrice() const { return close_price; }
    double getHighPrice() const { return high_price; }
    double getLowPrice() const { return low_price; }
    double getVolume() const { return volume; }
};

#endif // ASSET_H
