/**
 * Tailwind config для AI Студия Че.
 * Палитра вытащена из inline-scripts в views/*.html (там идентичная).
 *
 * Build: `npx tailwindcss -i ./views/input.css -o ./views/styles.css --minify`
 * Production: подключается через <link rel="stylesheet" href="/styles.css"/>
 * вместо <script src="https://cdn.tailwindcss.com/3.4.0"></script>.
 */
module.exports = {
  darkMode: "class",
  content: [
    "./views/*.html",
    "./views/*.js",
  ],
  theme: {
    extend: {
      colors: {
        primary: "#ff8c42",
        "primary-dim": "#ff6b00",
        "primary-soft": "#ff8c42",
        secondary: "#ffb347",
        "surface-lo": "#181510",
        surface: "#1e1a14",
        surface2: "#272018",
        surface3: "#322a1e",
        surface4: "#3d3325",
        "on-surface": "#f0e6d8",
        "on-surface-dim": "#a89880",
        outline: "#4a3f2f",
        error: "#ff6b6b",
        background: "#141210",
      },
      fontFamily: {
        headline: ["Golos Text", "Manrope", "sans-serif"],
        body: ["Golos Text", "Inter", "sans-serif"],
        sans: ["Golos Text", "Inter", "sans-serif"],
      },
    },
  },
  plugins: [],
  // Safelist для классов, которые формируются динамически в JS (template strings,
  // данные из БД) и которые Tailwind не увидит при content-scan. Tightеним по мере
  // необходимости — пока широкий, потом сужаем после первого билда.
  safelist: [
    { pattern: /^(bg|text|border)-(primary|secondary|surface|error)(-dim|-soft|-lo|2|3|4)?$/ },
    { pattern: /^(bg|text)-on-surface(-dim)?$/ },
    { pattern: /^(grid-cols|gap|p|m|px|py|mx|my|w|h|max-w|min-h)-/, variants: ["sm", "md", "lg"] },
  ],
};
