// Core Three.js Setup
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(window.devicePixelRatio);
document.body.appendChild(renderer.domElement);

// Lighting
const ambientLight = new THREE.AmbientLight(0x404040, 2);
scene.add(ambientLight);
const pointLight = new THREE.PointLight(0xffffff, 2);
pointLight.position.set(10, 10, 10);
scene.add(pointLight);

// Market Spheres
const marketData = [
    { symbol: "AAPL", price: 150, color: 0xff0000 },
    { symbol: "TSLA", price: 900, color: 0x0000ff },
    { symbol: "BTC", price: 60000, color: 0xffd700 },
];

const stockObjects = [];

marketData.forEach((stock, index) => {
    const geometry = new THREE.SphereGeometry(stock.price / 1000, 32, 32);
    const material = new THREE.MeshStandardMaterial({ color: stock.color });
    const stockMesh = new THREE.Mesh(geometry, material);

    stockMesh.position.set(Math.random() * 5 - 2.5, Math.random() * 5 - 2.5, Math.random() * 5 - 2.5);
    stockMesh.userData = { name: stock.symbol, price: stock.price };

    scene.add(stockMesh);
    stockObjects.push(stockMesh);
});

// Hover Animation
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();

document.addEventListener('mousemove', (event) => {
    mouse.x = (event.clientX / window.innerWidth) * 2 - 1;
    mouse.y = -(event.clientY / window.innerHeight) * 2 + 1;

    raycaster.setFromCamera(mouse, camera);
    const intersects = raycaster.intersectObjects(stockObjects);

    stockObjects.forEach(stock => stock.scale.set(1, 1, 1)); // Reset
    if (intersects.length > 0) {
        intersects[0].object.scale.set(1.2, 1.2, 1.2);
    }
});

// Animation Loop
function animate() {
    requestAnimationFrame(animate);
    stockObjects.forEach((stock, i) => stock.rotation.y += 0.01 * (i + 1));
    renderer.render(scene, camera);
}
animate();
