# Compiler and flags
CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -pedantic -Iheaders -I/usr/include/rapidjson -I/usr/local/include/crow -lsqlite3

# Directories
SRC_DIR = src
OBJ_DIR = obj/src
BIN_DIR = bin

# Source files
SOURCES = $(wildcard $(SRC_DIR)/*.cpp)
OBJECTS = $(patsubst $(SRC_DIR)/%.cpp, $(OBJ_DIR)/%.o, $(SOURCES))

# Output executable
TARGET = $(BIN_DIR)/server

# Build rules
all: $(TARGET)

$(TARGET): $(OBJECTS)
	mkdir -p $(BIN_DIR)
	$(CXX) $(CXXFLAGS) $^ -o $@ -lcurl -lpthread

$(OBJ_DIR)/%.o: $(SRC_DIR)/%.cpp
	mkdir -p $(OBJ_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf $(OBJ_DIR) $(BIN_DIR)

.PHONY: all clean
