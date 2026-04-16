/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'bbg-bg': '#000000',
        'bbg-panel': '#0a0a0a',
        'bbg-border': '#1a1a1a',
        'bbg-green': '#00ff00',
        'bbg-red': '#ff0000',
        'bbg-cyan': '#00ffff'
      },
      fontFamily: {
        mono: ['"Courier New"', 'Courier', 'monospace'],
      }
    },
  },
  plugins: [],
}
