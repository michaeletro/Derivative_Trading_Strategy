#include "APIConnection.h"
#include <stdexcept>
#include <iostream>
#include "../libs/rapidjson/include/rapidjson/en.h"

size_t writeCallback(void* contents, size_t size, size_t nmemb, std::string* output) {
    size_t totalSize = size * nmemb;
    output->append(static_cast<char*>(contents), totalSize);
    return totalSize;
}

std::string APIConnection::fetchData(const std::string& url) {
    CURL* curl = curl_easy_init();
    if (!curl) throw std::runtime_error("Failed to initialize CURL");

    std::string response;
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);

    CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        curl_easy_cleanup(curl);
        throw std::runtime_error("CURL error: " + std::string(curl_easy_strerror(res)));
    }

    curl_easy_cleanup(curl);
    return response;
}

rapidjson::Document APIConnection::parseJSON(const std::string& jsonString) {
    rapidjson::Document document;
    if (document.Parse(jsonString.c_str()).HasParseError()) {
        throw std::runtime_error("Error parsing JSON: " +
                                 std::string(rapidjson::GetParseError_En(document.GetParseError())));
    }
    return document;
}
