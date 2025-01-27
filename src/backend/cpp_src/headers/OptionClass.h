#ifndef OPTION_CLASS_H
#define OPTION_CLASS_H

#include "AssetClass.h"

class OptionClass : public AssetClass {
protected:
    void validateParameters() const override;

public:
    OptionClass(const std::string& asset_name, const std::string& start_date = "2023-01-09",
                const std::string& end_date = "", const std::string& time_multiplier = "1",
                const std::string& time_span = "day", const std::string& sort = "asc",
                const std::string& api_key = "YOUR_API_KEY", int limit = 5000,
                bool adjusted = true, bool debug = true);
    virtual ~OptionClass() = default;

    void fetchOptionData();
};

#endif // OPTION_CLASS_H
