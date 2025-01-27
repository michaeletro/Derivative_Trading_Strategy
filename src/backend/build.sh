#!/bin/bash

echo "Building the REST API server..."
make

if [ $? -eq 0 ]; then
    echo "Starting the REST API server..."
    ./bin/server
else
    echo "Build failed. Unable to start the server."
fi
