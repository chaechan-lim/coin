/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        buy: '#10b981',
        sell: '#ef4444',
        neutral: '#6b7280',
      },
    },
  },
  plugins: [],
}
