#ifndef IBWEBSOCKET_H
#define IBWEBSOCKET_H

#include <iostream>
#include <string>
#include <thread>
#include <websocketpp/config/asio_client.hpp>
#include <websocketpp/client.hpp>

using websocketpp::connection_hdl;
using websocketpp::lib::placeholders::_1;
using websocketpp::lib::placeholders::_2;
using websocketpp::lib::bind;

class IBWebSocket {
public:
    IBWebSocket();
    ~IBWebSocket();

    void connect(const std::string& uri);
    void sendMessage(const std::string& message);
    void close();

private:
    using client = websocketpp::client<websocketpp::config::asio_tls_client>;

    client wsClient;
    websocketpp::connection_hdl wsHandle;
    std::thread wsThread;

    void onOpen(connection_hdl hdl);
    void onMessage(connection_hdl hdl, client::message_ptr msg);
    void onClose(connection_hdl hdl);
    void onFail(connection_hdl hdl);
};

#endif // IBWEBSOCKET_H
