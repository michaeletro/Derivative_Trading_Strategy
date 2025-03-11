document.addEventListener("DOMContentLoaded", () => {
    const darkModeToggle = document.getElementById("dark-mode-toggle");
    const marketContainer = document.getElementById("market-container");

    // Dummy Data for Market Prices
    const dummyMarketData = [
        { ticker: "AAPL", price: "$179.32", change: "+1.2%" },
        { ticker: "TSLA", price: "$910.10", change: "-0.5%" },
        { ticker: "BTC", price: "$38,450", change: "+2.3%" },
        { ticker: "ETH", price: "$2,250", change: "+1.8%" }
    ];

    // Render Market Prices
    function loadMarketData() {
        marketContainer.innerHTML = "";
        dummyMarketData.forEach(stock => {
            const stockItem = document.createElement("div");
            stockItem.className = "market-item";
            stockItem.innerHTML = `
                <span>${stock.ticker}</span> 
                <span>${stock.price}</span> 
                <span style="color: ${stock.change.includes('+') ? 'lime' : 'red'};">${stock.change}</span>
            `;
            marketContainer.appendChild(stockItem);
        });
    }
    loadMarketData();

    // Dark Mode Toggle
    darkModeToggle.addEventListener("click", () => {
        document.body.classList.toggle("dark-mode");
        localStorage.setItem("darkMode", document.body.classList.contains("dark-mode"));
    });

    // Apply saved Dark Mode preference
    if (localStorage.getItem("darkMode") === "true") {
        document.body.classList.add("dark-mode");
    }
    const leaderboardContainer = document.getElementById("leaderboard-container");
    const portfolioContainer = document.getElementById("portfolio-container");
    const darkModeCheckbox = document.getElementById("dark-mode-checkbox");

    // Dummy Leaderboard Data
    const leaderboardData = [
        { name: "ElonTradeX", profit: "$120K" },
        { name: "CryptoKing98", profit: "$98K" },
        { name: "WallStreetWolf", profit: "$82K" },
    ];

    function loadLeaderboard() {
        if (!leaderboardContainer) return;
        leaderboardContainer.innerHTML = "";
        leaderboardData.forEach((user, index) => {
            const listItem = document.createElement("li");
            listItem.innerHTML = `#${index + 1} - ${user.name} (${user.profit})`;
            leaderboardContainer.appendChild(listItem);
        });
    }
    loadLeaderboard();

    // Dummy Portfolio Data
    const portfolioData = [
        { asset: "AAPL", shares: 10, value: "$1790" },
        { asset: "TSLA", shares: 5, value: "$4550" },
        { asset: "BTC", shares: 0.2, value: "$7700" },
    ];

    function loadPortfolio() {
        if (!portfolioContainer) return;
        portfolioContainer.innerHTML = "";
        portfolioData.forEach((asset) => {
            const assetCard = document.createElement("div");
            assetCard.className = "glass";
            assetCard.innerHTML = `<h3>${asset.asset}</h3> <p>${asset.shares} shares - ${asset.value}</p>`;
            portfolioContainer.appendChild(assetCard);
        });
    }
    loadPortfolio();

    // Dark Mode Toggle
    if (darkModeCheckbox) {
        darkModeCheckbox.checked = localStorage.getItem("darkMode") === "true";
        darkModeCheckbox.addEventListener("change", () => {
            document.body.classList.toggle("dark-mode", darkModeCheckbox.checked);
            localStorage.setItem("darkMode", darkModeCheckbox.checked);
        });
    }

    if (localStorage.getItem("darkMode") === "true") {
        document.body.classList.add("dark-mode");
    }
    // Settings Save
    const saveButton = document.getElementById("save-settings");
    if (saveButton) {
        saveButton.addEventListener("click", () => {
            alert("Settings saved successfully!");
        });
    }
    // Notifications
    const notificationsCheckbox = document.getElementById("notifications-checkbox");
    if (notificationsCheckbox) {
        notificationsCheckbox.checked = localStorage.getItem("notifications") === "true";
        notificationsCheckbox.addEventListener("change", () => {
            localStorage.setItem("notifications", notificationsCheckbox.checked);
        });
    }
    // Simulate XP Progress
    const xpProgress = document.getElementById("xp-progress");
    const levelDisplay = document.getElementById("level-display");
    if (xpProgress && levelDisplay) {
        setTimeout(() => {
            xpProgress.style.width = "75%";
            xpProgress.textContent = "75%";
            levelDisplay.textContent = "5";
        }, 1000);
    }
    // Simulate News Feed
    const newsFeed = document.getElementById("news-feed");
    if (newsFeed) {
        const newsItems = [
            { title: "Market hits record high", content: "The stock market reached an all-time high today, driven by gains in tech stocks." },
            { title: "Crypto prices surge", content: "Bitcoin and Ethereum see significant price increases as institutional interest grows." },
            { title: "New trading app launched", content: "A new trading app promises to revolutionize the way we invest in the stock market." }
        ];
        newsItems.forEach(news => {
            const newsItem = document.createElement("div");
            newsItem.className = "news-item";
            newsItem.innerHTML = `<h4>${news.title}</h4> <p>${news.content}</p>`;
            newsFeed.appendChild(newsItem);
        });
    }
    // Simulate Notifications
    const notifications = document.getElementById("notifications");
    if (notifications) {
        const notificationItems = [
            { message: "You have a new follower!", time: "2 mins ago" },
            { message: "Your portfolio value has increased by 5%", time: "10 mins ago" },
            { message: "New comment on your post", time: "30 mins ago" }
        ];
        notificationItems.forEach(notification => {
            const notificationItem = document.createElement("div");
            notificationItem.className = "notification-item";
            notificationItem.innerHTML = `<p>${notification.message}</p> <span>${notification.time}</span>`;
            notifications.appendChild(notificationItem);
        });
    }
    // Simulate Transactions
    const transactions = document.getElementById("transactions");
    if (transactions) {
        const transactionItems = [
            { type: "Buy", asset: "AAPL", shares: 10, price: "$1790" },
            { type: "Sell", asset: "TSLA", shares: 5, price: "$4550" },
            { type: "Buy", asset: "BTC", shares: 0.2, price: "$7700" }
        ];
        transactionItems.forEach(transaction => {
            const transactionItem = document.createElement("div");
            transactionItem.className = "transaction-item";
            transactionItem.innerHTML = `<p>${transaction.type} - ${transaction.asset} - ${transaction.shares} shares - ${transaction.price}</p>`;
            transactions.appendChild(transactionItem);
        });
    }
    // Simulate Chat
    const chatContainer = document.getElementById("chat-container");
    if (chatContainer) {
        const chatMessages = [
            "Hi, how's the market today?",
            "It's looking good! Tech stocks are up.",
            "Any recommendations for new trades?",
            "Consider looking into renewable energy stocks."
        ];
        chatMessages.forEach(message => {
            const chatMessage = document.createElement("div");
            chatMessage.className = "chat-message";
            chatMessage.textContent = message;
            chatContainer.appendChild(chatMessage);
        });
    }
    // Simulate User Profile
    const userProfile = document.getElementById("user-profile");
    if (userProfile) {
        userProfile.innerHTML = `
            <h3>John Doe</h3>
            <p>Level: 5</p>
            <p>XP: 75%</p>
            <p>Followers: 120</p>
            <p>Following: 80</p>
        `;
    }
    // Simulate Settings
    const settingsContainer = document.getElementById("settings-container");
    if (settingsContainer) {
        settingsContainer.innerHTML = `
            <h3>Settings</h3>
            <label>
                <input type="checkbox" id="dark-mode-checkbox"> Dark Mode
            </label>
            <label>
                <input type="checkbox" id="notifications-checkbox"> Notifications
            </label>
            <button id="save-settings">Save Settings</button>
        `;
    }
    // Simulate Footer
    const footer = document.getElementById("footer");
    if (footer) {
        footer.innerHTML = `
            <p>&copy; 2023 Stock Trading App. All rights reserved.</p>
        `;
    }
    // Simulate Header
    const header = document.getElementById("header");
    if (header) {
        header.innerHTML = `
            <h1>Stock Trading App</h1>
            <nav>
                <a href="#market">Market</a>
                <a href="#portfolio">Portfolio</a>
                <a href="#leaderboard">Leaderboard</a>
                <a href="#settings">Settings</a>
            </nav>
        `;
    }
    // Simulate Market
    const market = document.getElementById("market");
    if (market) {
        market.innerHTML = `
            <h2>Market Prices</h2>
            <div id="market-container"></div>
        `;
    }
    // Simulate Portfolio
    const portfolio = document.getElementById("portfolio");
    if (portfolio) {
        portfolio.innerHTML = `
            <h2>Portfolio</h2>
            <div id="portfolio-container"></div>
        `;
    }
    // Simulate Leaderboard
    const leaderboard = document.getElementById("leaderboard");
    if (leaderboard) {
        leaderboard.innerHTML = `
            <h2>Leaderboard</h2>
            <ul id="leaderboard-container"></ul>
        `;
    }
    // Simulate News
    const news = document.getElementById("news");
    if (news) {
        news.innerHTML = `
            <h2>News Feed</h2>
            <div id="news-feed"></div>
        `;
    }

});
