// "Pixel Pop" — the retro-handheld look shared by the web and mobile apps.
//
// Night-mode handheld palette: deep indigo screen, chunky ink outlines with hard
// drop shadows, a red primary button, coin-gold highlights. Pixel type is used
// for display bits only (logo, titles, badges, numbers); body copy stays a
// friendly rounded sans so nothing is a chore to read.
//
// The web app mirrors these values as CSS custom properties in index.css — keep
// the two in step when a colour changes.

export const colors = {
  bg: "#14102b",
  bgElevated: "#1d1840",
  surface: "#2a2356",
  surfaceHover: "#352d6a",
  border: "#4a3f8f",
  ink: "#0a0717", // outlines + hard shadows
  text: "#f5f2ff",
  textMuted: "#b3acd9",
  primary: "#d62839", // white text on it: 4.97:1
  primaryDark: "#9e1b2a",
  gold: "#ffd23f",
  goldDark: "#b88a00",
  cyan: "#3fd0ff",
  green: "#5ee37f",
  orange: "#ffa53f",
  danger: "#ff6b6b",
  dangerBg: "rgba(255, 107, 107, 0.12)",
  onBright: "#1a1033", // text on gold/green/cyan
} as const;

export const radius = { sm: 4, md: 8, lg: 12 } as const;

/** Hard drop-shadow depth for chunky buttons/panels (px). */
export const shadowDepth = 4;

export const fonts = {
  /** Pixel display face — logo, headings, badges, numbers. */
  pixel: "PressStart2P",
  body: "Nunito",
  bodyBold: "NunitoBold",
  bodyBlack: "NunitoBlack",
} as const;

/** Card art aspect ratio (63×88mm). */
export const CARD_ASPECT = 63 / 88;
