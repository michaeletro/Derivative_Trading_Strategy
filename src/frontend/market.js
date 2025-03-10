document.addEventListener("DOMContentLoaded", function () {
    const marketContainer = document.getElementById("market-container");

    // Dummy market data (Replace with real API later)
    let marketData = [
        { ticker: "AAPL", price: 150.23, change: 0.5 },
        { ticker: "TSLA", price: 980.45, change: -1.2 },
        { ticker: "BTC", price: 45000.67, change: 2.5 },
        { ticker: "ETH", price: 3200.34, change: -0.8 }
    ];

    function renderMarketData() {
        marketContainer.innerHTML = ""; // Clear previous data
        marketData.forEach((stock) => {
            const stockElement = document.createElement("div");
            stockElement.classList.add("market-item");

            let changeColor = stock.change >= 0 ? "green" : "red";
            let arrow = stock.change >= 0 ? "▲" : "▼";

            stockElement.innerHTML = `
                <span>${stock.ticker}</span>
                <span>$${stock.price.toFixed(2)}</span>
                <span style="color: ${changeColor}">${arrow} ${stock.change.toFixed(2)}%</span>
            `;
            
            marketContainer.appendChild(stockElement);
        });
    }

    function updateMarketData() {
        marketData = marketData.map(stock => {
            let randomChange = (Math.random() - 0.5) * 2; // Random value between -1 and 1
            let newPrice = stock.price + randomChange;
            return {
                ...stock,
                price: newPrice,
                change: ((newPrice - stock.price) / stock.price) * 100
            };
        });
        renderMarketData();
    }

    // Initial render
    renderMarketData();

    // Update market prices every 3 seconds
    setInterval(updateMarketData, 3000);

    function renderMarketData() {
        marketContainer.innerHTML = "";
        marketData.forEach((stock) => {
            const stockElement = document.createElement("div");
            stockElement.classList.add("market-item");

            stockElement.innerHTML = `
                <span>${stock.ticker}</span>
                <span>$${stock.price.toFixed(2)}</span>
                <button onclick="buyStock('${stock.ticker}', ${stock.price})">BUY</button>
                <button onclick="sellStock('${stock.ticker}', ${stock.price})">SELL</button>
            `;
            marketContainer.appendChild(stockElement);
        });
    }

    function buyStock(ticker, price) {
        if (userBalance >= price) {
            userBalance -= price;
            portfolio[ticker] = (portfolio[ticker] || 0) + 1;
            alert(`Bought 1 share of ${ticker}. New balance: $${userBalance.toFixed(2)}`);
        } else {
            alert("Not enough balance!");
        }
    }

    function sellStock(ticker, price) {
        if (portfolio[ticker] > 0) {
            userBalance += price;
            portfolio[ticker]--;
            alert(`Sold 1 share of ${ticker}. New balance: $${userBalance.toFixed(2)}`);
        } else {
            alert("You don't own this stock!");
        }
    }
    


    renderMarketData();
});
