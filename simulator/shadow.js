document.addEventListener("DOMContentLoaded", () => {
    // UI Elements
    const aiToggle = document.getElementById("aiToggle");
    const aiStatusText = document.getElementById("aiStatusText");
    const volSlider = document.getElementById("marketVol");
    const freqSlider = document.getElementById("regimeFreq");
    
    const statEquity = document.getElementById("statEquity");
    const statRegime = document.getElementById("statRegime");
    const regimeCard = document.getElementById("regimeCard");
    const statHurst = document.getElementById("statHurst");
    const statAtr = document.getElementById("statAtr");
    
    const epochContainer = document.getElementById("epochContainer");
    const epochProgress = document.getElementById("epochProgress");

    // Formatters
    const formatMoney = (val) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
    
    // Chart Setup
    const ctx = document.getElementById("liveChart").getContext("2d");
    Chart.defaults.color = "#8b949e";
    Chart.defaults.font.family = "'Outfit', sans-serif";

    // Data buffers
    const MAX_POINTS = 150;
    const historyData = [];
    const historyRegimes = []; // holds regime ID for coloring
    
    // Regimes Setup
    const REGIMES = {
        TRENDING: { id: 0, color: 'rgba(0, 242, 254, 0.15)', name: "TRENDING" },
        CHOPPY: { id: 1, color: 'rgba(255, 179, 0, 0.15)', name: "CHOPPY" },
        VOLATILE: { id: 2, color: 'rgba(255, 23, 68, 0.15)', name: "VOLATILE" }
    };
    const regimeKeys = Object.keys(REGIMES);

    // Initial Simulation State
    let equity = 100000.0;
    let currentHurst = 0.65;
    let currentAtr = 0.25;
    let activeRegime = REGIMES.TRENDING;
    let ticksSinceShift = 0;
    let isOptimizing = false;

    // Fill initial buffer
    for(let i=0; i<MAX_POINTS; i++) {
        historyData.push(equity);
        historyRegimes.push(activeRegime);
    }

    // Chart Background Plugin to segment vertically by regime
    const customBackgroundPlugin = {
        id: 'customBackgroundPlugin',
        beforeDraw: (chart) => {
            const ctx = chart.ctx;
            const xAxis = chart.scales.x;
            const yAxis = chart.scales.y;
            const top = yAxis.top;
            const bottom = yAxis.bottom;
            
            ctx.save();
            // We loop through points. But since this is a line chart without strict bar widths,
            // we paint rectangles between consecutive points.
            const meta = chart.getDatasetMeta(0);
            if (!meta.data || meta.data.length < 2) { ctx.restore(); return; }

            for (let i = 0; i < historyRegimes.length - 1; i++) {
                const regime = historyRegimes[i];
                if (!regime) continue;
                
                try {
                    const xStart = meta.data[i].x;
                    const xEnd = meta.data[i+1]? meta.data[i+1].x : xAxis.right;
                    // Slightly overlap to prevent white gaps
                    ctx.fillStyle = regime.color;
                    ctx.fillRect(Math.floor(xStart), top, Math.ceil(xEnd - xStart) + 1, bottom - top);
                } catch(e) {}
            }
            ctx.restore();
        }
    };

    const gradient = ctx.createLinearGradient(0, 0, 0, 400);
    gradient.addColorStop(0, '#8a2be2'); // primary
    gradient.addColorStop(1, '#00f2fe'); // secondary

    const liveChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: new Array(MAX_POINTS).fill(''),
            datasets: [{
                label: 'Equity',
                data: historyData,
                borderColor: '#ffffff',
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false, // Turn off native animation for pure feed feel
            interaction: { intersect: false, mode: 'index' },
            plugins: {
                legend: { display: false },
                tooltip: { enabled: false }
            },
            scales: {
                x: { grid: { display: false }, ticks: { display: false } },
                y: { grid: { color: 'rgba(255,255,255,0.05)' }, 
                     min: 90000, max: 110000, // will auto-adjust programmatically
                     ticks: {
                        callback: val => '$' + (val/1000).toFixed(0) + 'k'
                     }
                }
            }
        },
        plugins: [customBackgroundPlugin]
    });

    // Events
    aiToggle.addEventListener('change', (e) => {
        if(e.target.checked) {
            aiStatusText.textContent = "ONLINE";
            aiStatusText.className = "status-on";
        } else {
            aiStatusText.textContent = "OFFLINE";
            aiStatusText.className = "status-off";
        }
    });

    // Helper: Determine Win Rate based on alignment of DNA vs Regime
    const calculateWinProbability = () => {
        // Base edge is 55%. If unaligned, drops to 45% (losing edge).
        let isAligned = false;
        
        switch (activeRegime.name) {
            case "TRENDING":
                isAligned = (currentHurst > 0.60 && currentAtr < 0.30);
                break;
            case "CHOPPY":
                isAligned = (currentHurst < 0.55 && currentAtr < 0.30);
                break;
            case "VOLATILE":
                isAligned = (currentAtr >= 0.30); // Needs wide ATR filter
                break;
        }

        return isAligned ? 0.54 : 0.47;
    };

    // Optimization Progress Visualizer
    const triggerOptimization = () => {
        isOptimizing = true;
        epochContainer.style.opacity = 1;
        let progress = 0;
        epochProgress.style.width = '0%';
        
        const epochInterval = setInterval(() => {
            progress += 5; // 20 ticks = 1 second
            epochProgress.style.width = `${progress}%`;
            
            if (progress >= 100) {
                clearInterval(epochInterval);
                epochContainer.style.opacity = 0;
                
                // Roll new DNA matching the regime
                switch(activeRegime.name) {
                    case "TRENDING":
                        currentHurst = (0.61 + Math.random() * 0.14).toFixed(4);
                        currentAtr = (0.10 + Math.random() * 0.15).toFixed(4);
                        break;
                    case "CHOPPY":
                        currentHurst = (0.35 + Math.random() * 0.15).toFixed(4);
                        currentAtr = (0.10 + Math.random() * 0.15).toFixed(4);
                        break;
                    case "VOLATILE":
                        currentHurst = (0.4 + Math.random() * 0.3).toFixed(4);
                        currentAtr = (0.35 + Math.random() * 0.2).toFixed(4);
                        break;
                }
                
                statHurst.textContent = currentHurst;
                statAtr.textContent = currentAtr;
                
                // Flash styling
                statHurst.style.color = '#00f2fe'; statAtr.style.color = '#00f2fe';
                setTimeout(()=> { statHurst.style.color=''; statAtr.style.color=''; }, 500);
                
                isOptimizing = false;
            }
        }, 50);
    };

    let tickCounter = 0;
    // Step Engine
    const simulationTick = () => {
        tickCounter++;
        const volSliderVal = parseInt(volSlider.value);
        const freqSliderVal = parseInt(freqSlider.value);

        // 1. Regime Shift check
        ticksSinceShift++;
        const shiftProbability = (freqSliderVal * 0.002); // e.g. 5 = 1% chance per tick
        
        if (ticksSinceShift > 50 && Math.random() < shiftProbability) {
            ticksSinceShift = 0;
            const newRegimeKey = regimeKeys[Math.floor(Math.random() * regimeKeys.length)];
            activeRegime = REGIMES[newRegimeKey];
            statRegime.textContent = activeRegime.name;
            
            // Visual pulse on dashboard
            regimeCard.style.boxShadow = `0 0 20px ${activeRegime.color}`;
            setTimeout(()=> regimeCard.style.boxShadow='none', 1000);

            // Trigger Shadow Optimizer if ON
            if (aiToggle.checked) {
                triggerOptimization();
            }
        }

        // 2. Monte Carlo Equity Generation
        // If optimizing, halt trading (or suffer temporary bad edge)
        // We'll suffer bad edge for a brief moment while it optimizes
        const currentWinProb = calculateWinProbability();
        
        // Volatility multiplier
        const baseRisk = 150; 
        const actualRisk = baseRisk * (volSliderVal * 0.5); // scales magnitude of each tick

        const isWin = Math.random() < currentWinProb;
        if (isWin) {
            equity += actualRisk;
        } else {
            equity -= actualRisk;
        }

        // Keep buffer size
        historyData.shift();
        historyData.push(equity);
        
        historyRegimes.shift();
        historyRegimes.push(activeRegime);

        // Render Updates
        statEquity.textContent = formatMoney(equity);
        
        // Dynamic Chart Y-axis adjustment
        const maxEq = Math.max(...historyData);
        const minEq = Math.min(...historyData);
        liveChart.options.scales.y.max = maxEq + (actualRisk * 20);
        liveChart.options.scales.y.min = minEq - (actualRisk * 20);
        
        // Render
        liveChart.update();
    };

    // Run Engine at 20 frames per second (50ms interval) for rapid feel
    setInterval(simulationTick, 50);
});
