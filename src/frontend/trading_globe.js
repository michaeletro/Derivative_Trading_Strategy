document.addEventListener("DOMContentLoaded", function() {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });

    renderer.setSize(window.innerWidth, window.innerHeight);
    document.getElementById("canvas-container").appendChild(renderer.domElement);

    // Add a globe
    const geometry = new THREE.SphereGeometry(2, 64, 64);
    const material = new THREE.MeshStandardMaterial({ color: 0x003366, wireframe: true });
    const globe = new THREE.Mesh(geometry, material);
    scene.add(globe);

    // Lights
    const ambientLight = new THREE.AmbientLight(0xffffff, 1);
    scene.add(ambientLight);

    // Camera positioning
    camera.position.z = 5;

    // Animation
    function animate() {
        requestAnimationFrame(animate);
        globe.rotation.y += 0.005;
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
