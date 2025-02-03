CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -pedantic -Iheaders -I/usr/include/rapidjson -I/usr/local/include/crow
LDFLAGS = -lcurl -lpthread -lsqlite3  # Ensure -lsqlite3 is at the very end

SRC_DIR = src
OBJ_DIR = obj
BIN_DIR = bin

SOURCES = $(wildcard $(SRC_DIR)/*.cpp)
OBJECTS = $(patsubst $(SRC_DIR)/%.cpp, $(OBJ_DIR)/%.o, $(SOURCES))

EXECUTABLE = $(BIN_DIR)/server

all: $(EXECUTABLE)

$(EXECUTABLE): $(OBJECTS)
	mkdir -p $(BIN_DIR)
	$(CXX) $(CXXFLAGS) $(OBJECTS) -o $(EXECUTABLE) $(LDFLAGS)  # Ensure -lsqlite3 is at the end

$(OBJ_DIR)/%.o: $(SRC_DIR)/%.cpp
	mkdir -p $(OBJ_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf $(OBJ_DIR) $(BIN_DIR)
