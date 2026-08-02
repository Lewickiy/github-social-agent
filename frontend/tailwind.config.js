/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // GitHub Primer light palette
        canvas: "#ffffff",
        "canvas-subtle": "#f6f8fa",
        border: "#d0d7de",
        "border-muted": "#d8dee4",
        fg: "#1f2328",
        "fg-muted": "#656d76",
        "fg-subtle": "#8c959f",
        accent: "#0969da",
        "accent-emphasis": "#0969da",
        "btn-bg": "#f6f8fa",
        "btn-hover": "#f3f4f6",
        "btn-border": "#d0d7de",
        success: "#1a7f37",
        "success-fg": "#1a7f37",
        "success-emphasis": "#1f883d",
        "success-subtle": "#dafbe1",
        danger: "#cf222e",
        "danger-fg": "#cf222e",
        "danger-subtle": "#ffebe9",
        attention: "#9a6700",
        "attention-subtle": "#fff8c5",
        done: "#8250df",
        "done-subtle": "#fbefff",
        header: "#24292f",
        "header-fg": "#ffffff",
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          '"Noto Sans"',
          "Helvetica",
          "Arial",
          "sans-serif",
          '"Apple Color Emoji"',
          '"Segoe UI Emoji"',
        ],
        mono: [
          '"SF Mono"',
          "ui-monospace",
          '"Cascadia Code"',
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        card: "0 1px 0 rgba(27,31,36,0.04)",
        overlay:
          "0 1px 3px rgba(27,31,36,0.12), 0 8px 24px rgba(66,74,83,0.12)",
        focus: "0 0 0 3px rgba(9,105,218,0.3)",
      },
      borderRadius: {
        md: "6px",
      },
    },
  },
  plugins: [],
};
