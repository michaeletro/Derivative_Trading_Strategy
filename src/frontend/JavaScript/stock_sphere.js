document.addEventListener("DOMContentLoaded", function() {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(window.innerWidth, window.innerHeight);

    // Attach the renderer to the HTML container
    document.getElementById("canvas-container").appendChild(renderer.domElement);

    // Lighting
    const light = new THREE.PointLight(0xffffff, 1.5);
    light.position.set(5, 5, 5);
    scene.add(light);

    // Dummy stock data
    const stocks = [
        { symbol: "AAPL", marketCap: 2.5, color: 0xff5733 },
        { symbol: "TSLA", marketCap: 1.2, color: 0x33ff57 },
        { symbol: "BTC", marketCap: 1.8, color: 0x5733ff }
    ];

    // Generate spheres
    stocks.forEach((stock, i) => {
        const geometry = new THREE.SphereGeometry(stock.marketCap / 2, 32, 32);
        const material = new THREE.MeshStandardMaterial({ color: stock.color });
        const sphere = new THREE.Mesh(geometry, material);

        sphere.position.set(Math.random() * 5 - 2.5, Math.random() * 5 - 2.5, Math.random() * 5 - 2.5);
        scene.add(sphere);
    });

    // Camera positioning
    camera.position.z = 5;

    // Animation Loop
    function animate() {
        requestAnimationFrame(animate);
        scene.rotation.y += 0.002;
        renderer.render(scene, camera);
    }
    animate();

    // Resize handling
    window.addEventListener('resize', () => {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
    });
});
