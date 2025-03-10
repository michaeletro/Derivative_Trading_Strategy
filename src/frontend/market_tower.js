document.addEventListener("DOMContentLoaded", function() {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });

    renderer.setSize(window.innerWidth, window.innerHeight);
    document.getElementById("canvas-container").appendChild(renderer.domElement);

    // Market Data
    const stocks = [
        { symbol: "AAPL", price: 150, color: 0xff0000 },
        { symbol: "TSLA", price: 900, color: 0x0000ff },
        { symbol: "BTC", price: 60000, color: 0xffd700 }
    ];

    // Create bars
    stocks.forEach((stock, i) => {
        const geometry = new THREE.BoxGeometry(1, stock.price / 100, 1);
        const material = new THREE.MeshStandardMaterial({ color: stock.color });
        const bar = new THREE.Mesh(geometry, material);
        
        bar.position.x = i * 2;
        scene.add(bar);
    });

    // Lights
    const light = new THREE.PointLight(0xffffff, 1.5);
    light.position.set(5, 5, 5);
    scene.add(light);

    // Camera positioning
    camera.position.z = 5;
    camera.position.y = 3;

    // Animation
    function animate() {
        requestAnimationFrame(animate);
        scene.rotation.y += 0.002;
        renderer.render(scene, camera);
    }
    animate();
});
