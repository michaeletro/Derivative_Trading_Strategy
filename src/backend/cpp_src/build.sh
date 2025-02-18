#!/bin/bash

echo "🧠 Building Quant Program..."
make

if [ $? -eq 0 ]; then
    echo "🏁 Build successful. Starting the server..."
    ./bin/server
    echo "🚀 Server is now running at http://127.0.0.1:8080" 
else
    echo "❌ Build failed."
fi
