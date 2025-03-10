document.addEventListener("DOMContentLoaded", () => {
    const darkModeToggle = document.getElementById("toggle-dark-mode");
    const notificationsToggle = document.getElementById("toggle-notifications");

    // Load saved settings
    darkModeToggle.checked = localStorage.getItem("darkMode") === "true";
    notificationsToggle.checked = localStorage.getItem("notifications") === "true";

    darkModeToggle.addEventListener("change", () => {
        document.body.classList.toggle("dark-mode", darkModeToggle.checked);
        localStorage.setItem("darkMode", darkModeToggle.checked);
    });

    notificationsToggle.addEventListener("change", () => {
        localStorage.setItem("notifications", notificationsToggle.checked);
    });
   function updateSettings() {
        const darkMode = localStorage.getItem("darkMode") === "true";
        const notifications = localStorage.getItem("notifications") === "true";

        document.body.classList.toggle("dark-mode", darkMode);
        darkModeToggle.checked = darkMode;
        notificationsToggle.checked = notifications;
    }

    updateSettings();
    function toggleDarkMode() {
        document.body.classList.toggle("dark-mode");
        localStorage.setItem("darkMode", document.body.classList.contains("dark-mode"));
    }
    function toggleNotifications() {
        localStorage.setItem("notifications", notificationsToggle.checked);
    }
    darkModeToggle.addEventListener("change", toggleDarkMode);
    notificationsToggle.addEventListener("change", toggleNotifications);
    function updateSettings() {
        const darkMode = localStorage.getItem("darkMode") === "true";
        const notifications = localStorage.getItem("notifications") === "true";

        document.body.classList.toggle("dark-mode", darkMode);
        darkModeToggle.checked = darkMode;
        notificationsToggle.checked = notifications;
    }
});