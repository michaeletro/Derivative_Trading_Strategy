#ifndef API_CONNECTION_H
#define API_CONNECTION_H

#include <string>

class APIConnection {
public:
    static std::string fetchData(const std::string& url);
    static rapidjson::Document parseJSON(const std::string& jsonString);
};

#endif
