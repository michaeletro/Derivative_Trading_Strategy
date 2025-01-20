#include <iostream>
#include <curl/curl.h>
#include <sstream>
#include <map>
#include <string>

static size_t WriteCallBack(void* contents, size_t size, size_t nmemb, std::string* output) {
    size_t totalSize = size * nmemb;
    output->append((char*)contents, totalSize);
    return totalSize;
}

std::string generateAPIEndpoint(const std::string& baseURL, const std::string& endpoint, const std::map<std::string, std::string>& query_params){
    std::ostringstream endpoint;
    if(!baseURL.empty() && baseURL.back() =='/'){
        endpoint << baseURL.substr(0, baseURL.size() - 1);
    } else {
        endpoint << base_url;
    }

}

int main() {
    CURL* curl;
    CURLcode res;
    std::string readBuffer;
    std::string endpoint;

    curl = curl_easy_init();
    if (curl) {
        curl_easy_setopt(curl, CURLOPT_URL, api_endpoint);
    }

    return 0;
}
