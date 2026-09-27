/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/templates/**/*.html', './app/static/**/*.js'],
  theme: {
    extend: {
      colors: {
        canvas: '#FAF8F5',
        line: '#ECE6DC',
        ink: { DEFAULT: '#16373A', soft: '#5F7476' },
        peacock: { DEFAULT: '#0F5E5C', dark: '#0B4745', 50: '#E8F2F0', 100: '#D3E7E3' },
        marigold: { DEFAULT: '#E9A23B', dark: '#B97813', 50: '#FDF4E3' },
        rise: { DEFAULT: '#2E8B57', 50: '#E9F5EE' },
        fall: { DEFAULT: '#C2475A', 50: '#FBECEE' },
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans: ['Figtree', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        soft: '0 1px 2px rgba(22,55,58,.04), 0 8px 24px -12px rgba(22,55,58,.12)',
      },
    },
  },
  plugins: [],
};
