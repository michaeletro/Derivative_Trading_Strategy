#ifndef API_STRING_GENERATOR_H
#define API_STRING_GENERATOR_H

#include <string>
#include <map>

class APIStringGenerator {
public:
    static std::string generateURL(const std::string& base_url,
                                   const std::map<std::string, std::string>& query_params);
};

#endif // API_STRING_GENERATOR_H
