#include "../headers/Utilities/server.h"
#include <exception>
#include <iostream>
int main() {
    try { startServer(); return 0; }
    catch (const std::exception& error) { std::cerr << "Application failed: " << error.what() << '\n'; return 1; }
}
