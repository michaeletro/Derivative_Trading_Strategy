document.addEventListener("DOMContentLoaded", () => {
    const portfolioContainer = document.getElementById("portfolio-container");
    
    // Dummy portfolio data
    const portfolio = [
        { asset: "AAPL", shares: 10, price: "$150" },
        { asset: "TSLA", shares: 5, price: "$900" },
        { asset: "BTC", shares: 0.2, price: "$45,000" },
        { asset: "ETH", shares: 1.5, price: "$3,200" }
    ];
    portfolioContainer.innerHTML = "";

    portfolio.forEach(stock => {
        let div = document.createElement("div");
        div.classList.add("portfolio-item");
        div.innerHTML = `<span>${stock.asset} - ${stock.shares} shares</span><span>${stock.price}</span>`;
        portfolioContainer.appendChild(div);
    });
    portfolioContainer.addEventListener("click", (event) => {
        if (event.target.classList.contains("portfolio-item")) {
            alert(`You clicked on ${event.target.textContent}`);
        }
    });
    portfolioContainer.addEventListener("click", (event) => {
        if (event.target.classList.contains("portfolio-item")) {
            alert(`You clicked on ${event.target.textContent}`);
        }
    });
    // Settings
    const darkModeToggle = document.getElementById("toggle-dark-mode");
    const notificationsToggle = document.getElementById("toggle-notifications");


    // Simulating XP Progress
    let xpFill = document.getElementById("xp-progress");
    xpFill.style.width = "50%";
    xpFill.textContent = "50%";

    let level = document.getElementById("level");
    level.textContent = "3";
    // Simulate XP gain
    setTimeout(() => {
        xpFill.style.width = "60%";
        xpFill.textContent = "60%";
    }, 500);
    // Simulate XP gain

    setTimeout(() => {
        xpFill.style.width = "70%";
        xpFill.textContent = "70%";
        level.textContent = "4";
    }, 1000);

    function updateSettings() {
        const darkMode = localStorage.getItem("darkMode") === "true";
        const notifications = localStorage.getItem("notifications") === "true";

        document.body.classList.toggle("dark-mode", darkMode);
        darkModeToggle.checked = darkMode;
        notificationsToggle.checked = notifications;
    };
    function toggleNotifications() {
        localStorage.setItem("notifications", notificationsToggle.checked);
    };
    function toggleDarkMode() {
        document.body.classList.toggle("dark-mode");
        localStorage.setItem("darkMode", document.body.classList.contains("dark-mode"));
    };

});
