#ifndef WEBSOCKET_SERVER_H
#define WEBSOCKET_SERVER_H

#include "../libs/websocketpp/websocketpp/server.hpp"
#include "../libs/websocketpp/websocketpp/config/asio_no_tls.hpp"
#include <thread>
#include <unordered_map>
#include <mutex>
#include "../libs/rapidjson/include/rapidjson/document.h"

class WebSocketServer {
public:
    void run(int port);
    void sendMessage(websocketpp::connection_hdl hdl, const std::string& message);

private:
    websocketpp::server<websocketpp::config::asio> server;
    std::unordered_map<websocketpp::connection_hdl, rapidjson::Document,
                       std::hash<websocketpp::connection_hdl>,
                       std::equal_to<websocketpp::connection_hdl>> connections;
    std::mutex connectionMutex;

    void onOpen(websocketpp::connection_hdl hdl);
    void onClose(websocketpp::connection_hdl hdl);
    void onMessage(websocketpp::connection_hdl hdl, websocketpp::server<websocketpp::config::asio>::message_ptr msg);
};

#endif // WEBSOCKETSERVER_H