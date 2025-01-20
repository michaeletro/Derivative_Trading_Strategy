#include <iostream>
#include <curl/curl.h>
#include <sstream>
#include <map>
#include <string>

#include "headers/APIConnection.h"

/*
static size_t WriteCallBack(void* contents, size_t size, size_t nmemb, std::string* output) {
    size_t totalSize = size * nmemb;
    output->append((char*)contents, totalSize);
    return totalSize;
}

std::string generateAPIEndpoint(const std::string& baseURL, const std::string& endpoint, const std::map<std::string, std::string>& query_params){
    std::ostringstream api_endpoint;

    if(!baseURL.empty() && baseURL.back() =='/'){
        api_endpoint << baseURL.substr(0, baseURL.size() - 1);
    } else {
        api_endpoint << baseURL;
    }
    return api_endpoint.str();

}

int main() {
    CURL* curl = curl_easy_init();
    CURLcode res;
    std::string readBuffer;
    std::string api_endpoint;

    
    if (curl) {
        curl_easy_setopt(curl, CURLOPT_URL, api_endpoint); // Set your API endpoint here
        CURLcode res = curl_easy_perform(curl); // Perform the request, res will get the return code

        if (res != CURLE_OK) {
            std::cerr << "curl_easy_perform() failed: " << curl_easy_strerror(res) << std::endl;
        }
        curl_easy_cleanup(curl); // Always cleanup
    }
    return 0;
}

*/

int main() {
    APIConnection api();
    std::string url = api.buildURL();
    std::cout << "Generated API Endpoint: " << url << std::endl;
    return 0;
}