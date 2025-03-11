document.addEventListener("DOMContentLoaded", function() {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(window.innerWidth, window.innerHeight);
    document.getElementById("canvas-container").appendChild(renderer.domElement);

    // Load Earth texture
    const textureLoader = new THREE.TextureLoader();
    const earthTexture = textureLoader.load("../assets/earth_texture.jpg");
    const earthGeometry = new THREE.SphereGeometry(2, 64, 64);
    const earthMaterial = new THREE.MeshStandardMaterial({ map: earthTexture });
    const earth = new THREE.Mesh(earthGeometry, earthMaterial);
    scene.add(earth);

    // Financial market locations with dummy stock data
    const markets = [
        { name: "New York Stock Exchange", lat: 40.7128, lon: -74.0060, color: 0x00ff00, stocks: ["AAPL +2.3%", "TSLA -1.1%", "MSFT +0.5%"] },
        { name: "Nasdaq", lat: 40.7580, lon: -73.9855, color: 0xff0000, stocks: ["GOOGL +1.8%", "NVDA +3.2%", "AMZN -0.9%"] },
        { name: "London Stock Exchange", lat: 51.5074, lon: -0.1278, color: 0x0000ff, stocks: ["BP +0.7%", "HSBC -0.4%", "BARC +1.2%"] },
        { name: "Tokyo Stock Exchange", lat: 35.6895, lon: 139.6917, color: 0xffff00, stocks: ["Sony +2.1%", "Toyota +0.9%", "SoftBank -1.5%"] }
    ];

    const marketMarkers = [];document.addEventListener("DOMContentLoaded", function() {
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
        const renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setSize(window.innerWidth, window.innerHeight);
        document.getElementById("canvas-container").appendChild(renderer.domElement);
    
        // Load Earth texture
        const textureLoader = new THREE.TextureLoader();
        const earthTexture = textureLoader.load("../assets/earth_texture.jpg");
        const earthGeometry = new THREE.SphereGeometry(2, 64, 64);
        const earthMaterial = new THREE.MeshStandardMaterial({ map: earthTexture });
        const earth = new THREE.Mesh(earthGeometry, earthMaterial);
        scene.add(earth);
    
        // Financial market locations with dummy stock data
        const markets = [
            { name: "New York Stock Exchange", lat: 40.7128, lon: -74.0060, color: 0x00ff00, stocks: ["AAPL +2.3%", "TSLA -1.1%", "MSFT +0.5%"] },
            { name: "Nasdaq", lat: 40.7580, lon: -73.9855, color: 0xff0000, stocks: ["GOOGL +1.8%", "NVDA +3.2%", "AMZN -0.9%"] },
            { name: "London Stock Exchange", lat: 51.5074, lon: -0.1278, color: 0x0000ff, stocks: ["BP +0.7%", "HSBC -0.4%", "BARC +1.2%"] },
            { name: "Tokyo Stock Exchange", lat: 35.6895, lon: 139.6917, color: 0xffff00, stocks: ["Sony +2.1%", "Toyota +0.9%", "SoftBank -1.5%"] }
        ];
    
        const marketMarkers = [];
    
        markets.forEach(market => {
            const markerGeometry = new THREE.SphereGeometry(0.1, 32, 32);
            const markerMaterial = new THREE.MeshStandardMaterial({ color: market.color });
            const marker = new THREE.Mesh(markerGeometry, markerMaterial);
    
            // Convert lat/lon to 3D space
            const latRad = (90 - market.lat) * (Math.PI / 180);
            const lonRad = (market.lon + 180) * (Math.PI / 180);
    
            marker.position.set(
                2 * Math.sin(latRad) * Math.cos(lonRad),
                2 * Math.cos(latRad),
                2 * Math.sin(latRad) * Math.sin(lonRad)
            );
    
            scene.add(marker);
            marketMarkers.push({ marker, market });
        });
    
        // Lighting
        const light = new THREE.PointLight(0xffffff, 1.5);
        light.position.set(5, 5, 5);
        scene.add(light);
    
        // Camera positioning
        camera.position.z = 5;
    
        // Rotation speed
        let rotationSpeed = 0.002;
    
        // Tooltip
        const tooltip = document.getElementById("tooltip");
    
        function onMouseMove(event) {
            const mouse = new THREE.Vector2(
                (event.clientX / window.innerWidth) * 2 - 1,
                -(event.clientY / window.innerHeight) * 2 + 1
            );
    
            const raycaster = new THREE.Raycaster();
            raycaster.setFromCamera(mouse, camera);
    
            const intersects = raycaster.intersectObjects(marketMarkers.map(m => m.marker));
            if (intersects.length > 0) {
                const marketData = marketMarkers.find(m => m.marker === intersects[0].object).market;
                tooltip.style.left = `${event.clientX + 10}px`;
                tooltip.style.top = `${event.clientY + 10}px`;
                tooltip.innerHTML = `<b>${marketData.name}</b><br>${marketData.stocks.join("<br>")}`;
                tooltip.style.display = "block";
            } else {
                tooltip.style.display = "none";
            }
        }
    
        document.addEventListener("mousemove", onMouseMove);
    
        // Animation Loop
        function animate() {
            requestAnimationFrame(animate);
            earth.rotation.y += rotationSpeed;
            renderer.render(scene, camera);
        }
        animate();
    });
    

    markets.forEach(market => {
        const markerGeometry = new THREE.SphereGeometry(0.1, 32, 32);
        const markerMaterial = new THREE.MeshStandardMaterial({ color: market.color });
        const marker = new THREE.Mesh(markerGeometry, markerMaterial);

        // Convert lat/lon to 3D space
        const latRad = (90 - market.lat) * (Math.PI / 180);
        const lonRad = (market.lon + 180) * (Math.PI / 180);

        marker.position.set(
            2 * Math.sin(latRad) * Math.cos(lonRad),
            2 * Math.cos(latRad),
            2 * Math.sin(latRad) * Math.sin(lonRad)
        );

        scene.add(marker);
        marketMarkers.push({ marker, market });
    });

    // Lighting
    const light = new THREE.PointLight(0xffffff, 1.5);
    light.position.set(5, 5, 5);
    scene.add(light);

    // Camera positioning
    camera.position.z = 5;

    // Rotation speed
    let rotationSpeed = 0.002;

    // Tooltip
    const tooltip = document.getElementById("tooltip");

    function onMouseMove(event) {
        const mouse = new THREE.Vector2(
            (event.clientX / window.innerWidth) * 2 - 1,
            -(event.clientY / window.innerHeight) * 2 + 1
        );

        const raycaster = new THREE.Raycaster();
        raycaster.setFromCamera(mouse, camera);

        const intersects = raycaster.intersectObjects(marketMarkers.map(m => m.marker));
        if (intersects.length > 0) {
            const marketData = marketMarkers.find(m => m.marker === intersects[0].object).market;
            tooltip.style.left = `${event.clientX + 10}px`;
            tooltip.style.top = `${event.clientY + 10}px`;
            tooltip.innerHTML = `<b>${marketData.name}</b><br>${marketData.stocks.join("<br>")}`;
            tooltip.style.display = "block";
        } else {
            tooltip.style.display = "none";
        }
    }

    document.addEventListener("mousemove", onMouseMove);

    // Animation Loop
    function animate() {
        requestAnimationFrame(animate);
        earth.rotation.y += rotationSpeed;
        renderer.render(scene, camera);
    }
    animate();
});
