#include "APIStringGeneratorClass.h"
#include <sstream>

std::string APIStringGenerator::generateURL(const std::string& base_url,
                                            const std::map<std::string, std::string>& query_params) {
    std::ostringstream url_stream;
    url_stream << base_url;

    if (!query_params.empty()) {
        url_stream << "?";
        for (auto it = query_params.begin(); it != query_params.end(); ++it) {
            if (it != query_params.begin()) {
                url_stream << "&";
            }
            url_stream << it->first << "=" << it->second;
        }
    }

    return url_stream.str();
}
