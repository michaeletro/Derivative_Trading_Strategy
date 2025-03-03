#ifndef API_STRING_GENERATOR_H
#define API_STRING_GENERATOR_H

#include "../DataStructs/DataStructs.h"
#include <string>
#include <map>

class APIStringGenerator {
    public:
    // Generate Complete API URL
        static std::string generateURL(const QueryData& query);

    // Get API Endpoint string for given Query Type
        static std::string RestAPIQueryTypeGenerate(RestAPIQueryType query_type, bool debug = true);
};

#endif // API_STRING_GENERATOR_H
