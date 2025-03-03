#ifndef API_STRING_GENERATOR_H
#define API_STRING_GENERATOR_H

#include <string>
#include <map>
#include "../DataStructs/DataStructs.h"

class APIStringGenerator {
public:
    static std::string generateURL(const QueryData& query);
};

#endif // API_STRING_GENERATOR_H
