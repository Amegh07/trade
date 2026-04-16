import { useEffect, useRef } from 'react'
import { createChart } from 'lightweight-charts'

export default function EquityChart() {
  const chartContainerRef = useRef()
  const chartRef = useRef()
  const seriesRef = useRef()

  useEffect(() => {
    // 1. Initialize Chart
    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: 'transparent' },
        textColor: '#c5c6c7',
      },
      grid: {
        vertLines: { color: '#1a1a1a' },
        horzLines: { color: '#1a1a1a' },
      },
      rightPriceScale: {
        borderColor: '#1a1a1a',
      },
      timeScale: {
        borderColor: '#1a1a1a',
        timeVisible: true,
      },
      crosshair: {
        vertLine: { color: '#333', style: 0 },
        horzLine: { color: '#333', style: 0 }
      }
    })
    chartRef.current = chart

    // 2. Add Area Series
    const areaSeries = chart.addAreaSeries({
      lineColor: '#00e676',
      topColor: 'rgba(0, 230, 118, 0.4)',
      bottomColor: 'rgba(0, 230, 118, 0.0)',
      lineWidth: 2,
    })
    seriesRef.current = areaSeries

    // Handle resize
    const handleResize = () => {
      if (chartRef.current && chartContainerRef.current) {
        chartRef.current.applyOptions({
          width: chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight
        })
      }
    }
    window.addEventListener('resize', handleResize)
    setTimeout(handleResize, 100) // Initial sizing

    // Fetch Initial Data
    const updateData = async () => {
      try {
        const res = await fetch('/api/equity')
        const data = await res.json()
        if (data && data.length > 0) {
          areaSeries.setData(data)
          chart.timeScale().fitContent()
          const emptyOverlay = document.getElementById('equity-chart-empty-state')
          if (emptyOverlay) emptyOverlay.style.display = 'none'
        } else {
          areaSeries.setData([])
          const emptyOverlay = document.getElementById('equity-chart-empty-state')
          if (emptyOverlay) emptyOverlay.style.display = 'flex'
        }
      } catch (err) {
        console.error("Equity fetch error", err)
      }
    }

    updateData()
    const interval = setInterval(updateData, 5000)

    return () => {
      window.removeEventListener('resize', handleResize)
      clearInterval(interval)
      chart.remove()
    }
  }, [])

  return (
    <div className="relative w-full h-full">
      <div id="equity-chart-empty-state" className="absolute inset-0 items-center justify-center z-10 text-gray-600 bg-[#050505]/95 font-mono tracking-widest text-xs flex">
         WAITING FOR MARKET DATA...
      </div>
      <div ref={chartContainerRef} className="absolute inset-0" />
    </div>
  )
}
