document.addEventListener("DOMContentLoaded", function () {
    // === SCENE, CAMERA, RENDERER ===
    let scene = new THREE.Scene();
    let camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    let renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });

    let container = document.getElementById("chart-container");
    renderer.setSize(container.clientWidth, container.clientHeight);
    container.appendChild(renderer.domElement);

    camera.position.set(0, 5, 12);
    let controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    // === LIGHTING ===
    let ambientLight = new THREE.AmbientLight(0xffffff, 0.3);
    scene.add(ambientLight);
    let directionalLight = new THREE.DirectionalLight(0xffffff, 1);
    directionalLight.position.set(0, 10, 10);
    scene.add(directionalLight);

    // === ADD GRID & AXES ===
    let grid = new THREE.GridHelper(20, 20, 0x00ff00, 0x555555);
    scene.add(grid);

    let axesHelper = new THREE.AxesHelper(10);
    scene.add(axesHelper);

    // === GENERATE MARKET DATA ===
    function generateMarketData(count = 50) {
        let data = [];
        let price = 100;
        for (let i = 0; i < count; i++) {
            let open = price;
            let close = open + (Math.random() * 20 - 5);
            let high = Math.max(open, close) + Math.random()*20;
            let low = Math.min(open, close) - Math.random() *20;
            data.push({ open, close, high, low });
            price = close;
        }
        return data;
    }

    let marketData = generateMarketData();
    let candlesticks = [];

    // === CREATE 3D CANDLESTICKS ===
    function createCandlestick(data, index) {
        let width = 0.3;
        let height = Math.abs(data.close - data.open) * 0.1;
        let color = data.close >= data.open ? 0x00ff00 : 0xff0000;

        let geometry = new THREE.BoxGeometry(width, height, width);
        let material = new THREE.MeshStandardMaterial({ color });
        let candle = new THREE.Mesh(geometry, material);
        
        candle.position.set(index * 0.5 - marketData.length * 0.25, (data.close + data.open) / 2 * 0.05, 0);
        scene.add(candle);

        // Wick
        let wickGeometry = new THREE.CylinderGeometry(0.05, 0.05, (data.high - data.low) * 0.1);
        let wickMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff });
        let wick = new THREE.Mesh(wickGeometry, wickMaterial);
        wick.position.set(index * 0.5 - marketData.length * 0.25, (data.high + data.low) / 2 * 0.05, 0);
        scene.add(wick);

        candlesticks.push({ candle, wick });
    }

    marketData.forEach((data, i) => createCandlestick(data, i));

    // === ANIMATE THE CHART ===
    function animate() {
        requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // === AUTO-ROTATE EFFECT ===
    setInterval(() => {
        camera.position.x = Math.sin(Date.now() * 0.0005) * 12;
        camera.lookAt(0, 0, 0);
    }, 5);

    // === CHART.JS MARKET TREND LINE CHART ===
    let ctx = document.getElementById('market-line-chart').getContext('2d');
    let marketLineChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: marketData.map((_, i) => `T${i}`),
            datasets: [{
                label: 'Market Price',
                data: marketData.map(d => d.close),
                borderColor: '#00ff7f',
                backgroundColor: 'rgba(0, 255, 127, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { display: false },
                y: { beginAtZero: false }
            }
        }
    });

    // === SIMULATING REAL-TIME MARKET UPDATES ===
    function updateChart() {
        marketData.shift();
        let lastPrice = marketData[marketData.length - 1].close;
        let newCandle = {
            open: lastPrice,
            close: lastPrice + (Math.random() * 5 - 5),
            high: lastPrice + Math.random() * 5,
            low: lastPrice - Math.random() * 5
        };
        marketData.push(newCandle);

        candlesticks.forEach(({ candle, wick }, i) => {
            let data = marketData[i];
            candle.position.y = (data.close + data.open) / 2 * 0.05;
            wick.position.y = (data.high + data.low) / 2 * 0.05;
        });

        marketLineChart.data.labels.shift();
        marketLineChart.data.labels.push(`T${marketData.length}`);
        marketLineChart.data.datasets[0].data = marketData.map(d => d.close);
        marketLineChart.update();
    }

    setInterval(updateChart, 2000);
});
