document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const winRateSlider = document.getElementById("winRate");
    const numTradesSlider = document.getElementById("numTrades");
    const riskSlider = document.getElementById("riskPerTrade");
    const winRateVal = document.getElementById("winRateVal");
    const numTradesVal = document.getElementById("numTradesVal");
    const riskVal = document.getElementById("riskPerTradeVal");
    const runBtn = document.getElementById("runBtn");

    const statStart = document.getElementById("statStart");
    const statFinal = document.getElementById("statFinal");
    const statWins = document.getElementById("statWins");
    const statLosses = document.getElementById("statLosses");
    const statDrawdown = document.getElementById("statDrawdown");

    // Chart Setup
    const ctx = document.getElementById("equityChart").getContext("2d");
    let chartInstance = null;

    Chart.defaults.color = "#9aa0a6";
    Chart.defaults.font.family = "'Outfit', sans-serif";

    // Formatters
    const formatMoney = (val) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
    const formatPercent = (val) => val.toFixed(2) + "%";

    // UI Listeners for realtime value updates
    winRateSlider.addEventListener("input", (e) => winRateVal.textContent = parseFloat(e.target.value).toFixed(1));
    numTradesSlider.addEventListener("input", (e) => numTradesVal.textContent = e.target.value);
    riskSlider.addEventListener("input", (e) => riskVal.textContent = parseFloat(e.target.value).toFixed(1));

    // Core Simulation Logic
    const runSimulation = () => {
        // Collect parameters
        const winProb = parseFloat(winRateSlider.value) / 100.0;
        const totalTrades = parseInt(numTradesSlider.value);
        const riskFraction = parseFloat(riskSlider.value) / 100.0;
        const startCapital = 100000.0;

        let currentEquity = startCapital;
        let peakEquity = startCapital;
        let maxDrawdownPct = 0.0;
        
        let wins = 0;
        let losses = 0;

        const equityCurve = [startCapital];

        // Process Trades
        for (let i = 0; i < totalTrades; i++) {
            // Fixed fractional allocation: Risk amount is based on CURRENT equity
            const riskAmount = currentEquity * riskFraction;
            
            // Random bounded by probability
            const isWin = Math.random() < winProb;

            if (isWin) {
                currentEquity += riskAmount;  // Payoff Ratio 1:1
                wins++;
            } else {
                currentEquity -= riskAmount;
                losses++;
            }

            // Math constraint: Can't have negative equity with fixed fraction, but float precision safety
            if (currentEquity <= 0) currentEquity = 0;

            equityCurve.push(currentEquity);

            // Drawdown tracking
            if (currentEquity > peakEquity) {
                peakEquity = currentEquity;
            } else {
                const currentDrawdown = (peakEquity - currentEquity) / peakEquity;
                if (currentDrawdown > maxDrawdownPct) {
                    maxDrawdownPct = currentDrawdown;
                }
            }

            // If ruined
            if (currentEquity < 1.0) break;
        }

        updateDashboard(startCapital, currentEquity, wins, losses, maxDrawdownPct);
        renderChart(equityCurve);
    };

    // Update the Dashboard Metrics
    const updateDashboard = (start, final, wins, losses, maxDd) => {
        statStart.textContent = formatMoney(start);
        
        // CountUp animation for Final Equity
        animateValue(statFinal, parseFloat(statFinal.textContent.replace(/[^0-9.-]+/g,"")) || start, final, 1000, true);
        
        // CountUp animation for integers
        animateValue(statWins, parseInt(statWins.textContent) || 0, wins, 1000, false);
        animateValue(statLosses, parseInt(statLosses.textContent) || 0, losses, 1000, false);
        
        let ddPct = maxDd * 100;
        statDrawdown.textContent = formatPercent(ddPct);
        
        if (final < start) {
            statFinal.className = "danger";
        } else {
            statFinal.className = "highlight";
        }
    };

    // Helper for smooth number rolling
    const animateValue = (obj, start, end, duration, isCurrency) => {
        let startTimestamp = null;
        const step = (timestamp) => {
            if (!startTimestamp) startTimestamp = timestamp;
            const progress = Math.min((timestamp - startTimestamp) / duration, 1);
            const current = progress * (end - start) + start;
            obj.textContent = isCurrency ? formatMoney(current) : Math.floor(current);
            if (progress < 1) {
                window.requestAnimationFrame(step);
            }
        };
        window.requestAnimationFrame(step);
    };

    // Render line chart with Chart.js
    const renderChart = (dataArray) => {
        if (chartInstance) {
            chartInstance.destroy();
        }

        const labels = dataArray.map((_, i) => i);
        const gradient = ctx.createLinearGradient(0, 0, 0, 400);
        gradient.addColorStop(0, 'rgba(0, 242, 254, 0.4)');
        gradient.addColorStop(1, 'rgba(0, 242, 254, 0.0)');

        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Equity Curve',
                    data: dataArray,
                    borderColor: '#00f2fe',
                    backgroundColor: gradient,
                    borderWidth: 2,
                    pointRadius: 0, // hide points for speed reading HFT
                    fill: true,
                    tension: 0.1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: {
                    duration: 1200,
                    easing: 'easeOutQuart'
                },
                interaction: {
                    intersect: false,
                    mode: 'index',
                },
                plugins: {
                    legend: {
                        display: false
                    },
                    tooltip: {
                        enabled: true,
                        backgroundColor: 'rgba(0,0,0,0.8)',
                        titleFont: { family: 'Outfit', size: 14 },
                        bodyFont: { family: 'Outfit', size: 14, weight: 'bold' },
                        callbacks: {
                            label: function(context) {
                                return formatMoney(context.parsed.y);
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)',
                            drawBorder: false
                        },
                        ticks: {
                            maxTicksLimit: 10
                        }
                    },
                    y: {
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)',
                            drawBorder: false
                        },
                        ticks: {
                            callback: function(value) {
                                if (value >= 1000000) return '$' + (value / 1000000).toFixed(1) + 'M';
                                if (value >= 1000) return '$' + (value / 1000).toFixed(0) + 'k';
                                return '$' + value;
                            }
                        }
                    }
                }
            }
        });
    };

    // Run once on load
    runSimulation();

    // Re-run on button click
    runBtn.addEventListener("click", () => {
        // trigger subtle button pump
        runBtn.style.transform = "scale(0.95)";
        setTimeout(() => runBtn.style.transform = "none", 150);
        runSimulation();
    });
});
