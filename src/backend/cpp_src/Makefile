# Compiler
CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -O2 -Iheaders -Isrc -I/usr/include/eigen3 -I/usr/include/websocketpp -DCROW_ENABLE_BOOST -I/usr/include/boost
CXXFLAGS += -DCROW_ENABLE_BOOST -I/usr/include/boost

# Directories
SRC_DIR = src
OBJ_DIR = obj
BIN_DIR = bin
INCLUDE_DIR = headers

# Libraries
LIBS = -lsqlite3 -lcurl -lboost_system -lpthread -lboost_thread
LIBS += -lssl -lcrypto -lboost_filesystem -lboost_system -L/usr/local/lib
CPPFLAGS += -I/usr/include/websocketpp


# Find all .cpp files in subdirectories
SRCS = $(shell find $(SRC_DIR) -name "*.cpp")
OBJS = $(patsubst $(SRC_DIR)/%.cpp, $(OBJ_DIR)/%.o, $(SRCS))

# Executable target
TARGET = $(BIN_DIR)/server

# ANSI Colors
RESET  = \033[0m
GREEN  = \033[32m
BLUE   = \033[34m
CYAN   = \033[36m
YELLOW = \033[33m
RED    = \033[31m

# Emojis
COMPILING = 🛠️
BUILDING  = 🚀
CLEANING  = 🧹
SUCCESS   = ✅
ERROR     = ❌
ARROW     = ➜

# Default target
all: $(TARGET)
	@echo "$(GREEN)$(SUCCESS) Build complete! Run $(CYAN)$(TARGET)$(RESET)"

# Compile the executable
$(TARGET): $(OBJS)
	@mkdir -p $(BIN_DIR)
	@echo "$(BUILDING) $(YELLOW)Linking executable...$(RESET)"
	@$(CXX) $(CXXFLAGS) -o $@ $^ $(LIBS)
	@echo "$(GREEN)$(SUCCESS) Executable built successfully!$(RESET)"

# Compile each .cpp file into an object file
$(OBJ_DIR)/%.o: $(SRC_DIR)/%.cpp
	@mkdir -p $(dir $@)
	@echo "$(COMPILING) $(CYAN)Compiling$(RESET) $< $(ARROW) $(BLUE)$@$(RESET)"
	@$(CXX) $(CXXFLAGS) -I$(INCLUDE_DIR) -I$(SRC_DIR) -c $< -o $@

# Clean command
clean:
	@echo "$(CLEANING) $(RED)Cleaning project...$(RESET)"
	rm -rf $(OBJ_DIR) $(BIN_DIR)
	@echo "$(SUCCESS) $(GREEN)Cleanup complete!$(RESET)"

# Debug mode
debug: CXXFLAGS += -g
debug: all
	@echo "$(GREEN)$(SUCCESS) Debug build complete!$(RESET)"
	