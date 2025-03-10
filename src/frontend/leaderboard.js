document.addEventListener("DOMContentLoaded", () => {
    const leaderboardList = document.getElementById("leaderboard-list");
    leaderboardList.innerHTML = "";
    // Dummy leaderboard data
    const traders = [
        { name: "🚀 QuantumTrader", xp: "5000 XP" },
        { name: "📈 WallStreetWolf", xp: "4300 XP" },
        { name: "💎 DiamondHands", xp: "3900 XP" },
        { name: "🔥 CryptoKing", xp: "3600 XP" },
        { name: "📊 RiskTaker", xp: "3400 XP" }
    ];

    traders.forEach(trader => {
        let li = document.createElement("li");
        li.textContent = `${trader.name} - ${trader.xp}`;
        leaderboardList.appendChild(li);
    });

    // Settings
    const darkModeToggle = document.getElementById("toggle-dark-mode");
    const notificationsToggle = document.getElementById("toggle-notifications");

    // Simulating XP Progress
    let xpFill = document.getElementById("xp-progress");
    xpFill.style.width = "50%";
    xpFill.textContent = "50%";
    function updateSettings() {
        const darkMode = localStorage.getItem("darkMode") === "true";
        const notifications = localStorage.getItem("notifications") === "true";

        document.body.classList.toggle("dark-mode", darkMode);
        darkModeToggle.checked = darkMode;
        notificationsToggle.checked = notifications;
    }
    function toggleNotifications() {
        localStorage.setItem("notifications", notificationsToggle.checked);
    };
    function toggleDarkMode() {
        document.body.classList.toggle("dark-mode");
        localStorage.setItem("darkMode", document.body.classList.contains("dark-mode"));
    };
    const leaderboardContainer = document.getElementById("leaderboard-container");

    let leaderboardData = [
        { username: "TraderKing", xp: 2500, portfolioValue: 20000 },
        { username: "CryptoBoss", xp: 1800, portfolioValue: 15000 },
        { username: "WallStreetWolf", xp: 1200, portfolioValue: 12000 }
    ];

    function renderLeaderboard() {
        leaderboardContainer.innerHTML = "";
        leaderboardData.forEach((user, index) => {
            const userElement = document.createElement("div");
            userElement.classList.add("market-item");

            userElement.innerHTML = `
                <span>#${index + 1}</span>
                <span>${user.username}</span>
                <span>💰 $${user.portfolioValue.toFixed(2)}</span>
                <span>🔥 ${user.xp} XP</span>
            `;
            leaderboardContainer.appendChild(userElement);
        });
    }

    renderLeaderboard();

});
