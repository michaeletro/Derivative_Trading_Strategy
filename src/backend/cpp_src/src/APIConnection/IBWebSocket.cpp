#include "../../headers/APIConnection/IBWebSocket.h"

IBWebSocket::IBWebSocket() {
    wsClient.init_asio();
    wsClient.set_open_handler(bind(&IBWebSocket::onOpen, this, _1));
    wsClient.set_message_handler(bind(&IBWebSocket::onMessage, this, _1, _2));
    wsClient.set_close_handler(bind(&IBWebSocket::onClose, this, _1));
    wsClient.set_fail_handler(bind(&IBWebSocket::onFail, this, _1));
}

IBWebSocket::~IBWebSocket() {
    close();
}

void IBWebSocket::connect(const std::string& uri) {
    websocketpp::lib::error_code ec;
    auto conn = wsClient.get_connection(uri, ec);
    
    if (ec) {
        std::cerr << "WebSocket connection failed: " << ec.message() << std::endl;
        return;
    }

    wsHandle = conn->get_handle();
    wsClient.connect(conn);
    wsThread = std::thread([this]() { wsClient.run(); });
}

void IBWebSocket::sendMessage(const std::string& message) {
    websocketpp::lib::error_code ec;
    wsClient.send(wsHandle, message, websocketpp::frame::opcode::text, ec);
    if (ec) {
        std::cerr << "WebSocket send failed: " << ec.message() << std::endl;
    }
}

void IBWebSocket::close() {
    websocketpp::lib::error_code ec;
    wsClient.close(wsHandle, websocketpp::close::status::going_away, "Client shutdown", ec);
    if (wsThread.joinable()) {
        wsThread.join();
    }
}

void IBWebSocket::onOpen(connection_hdl hdl) {
    std::cout << "WebSocket Connection Established ✅" << std::endl;
}

void IBWebSocket::onMessage(connection_hdl hdl, client::message_ptr msg) {
    std::cout << "Received message: " << msg->get_payload() << std::endl;
}

void IBWebSocket::onClose(connection_hdl hdl) {
    std::cout << "WebSocket Connection Closed ❌" << std::endl;
}

void IBWebSocket::onFail(connection_hdl hdl) {
    std::cout << "WebSocket Connection Failed ❌" << std::endl;
}
