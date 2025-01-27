#!/bin/bash

echo "Building the REST API server..."
make

if [ $? -eq 0 ]; then
    echo "Build successful. Starting the server..."
    ./bin/server
else
    echo "Build failed. Check the error messages above."
fi
