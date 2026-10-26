// Line icons drawn for VODarr (24 × 24, 1.8 stroke), MIT like the rest of the project.
// No SF Symbols or Apple artwork (ADR-0008).

const PATHS = {
  compass: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "m15.5 8.5-2 5-5 2 2-5z"],
  layers: ["m12 3 9 5-9 5-9-5z", "m3 13 9 5 9-5"],
  gear: [
    "M18.9 9.9L21.4 10.4L21.4 13.6L18.9 14.1L18.4 15.4L19.8 17.5L17.5 19.8L15.4 18.4L14.1 18.9L13.6 21.4L10.4 21.4L9.9 18.9L8.6 18.4L6.5 19.8L4.2 17.5L5.6 15.4L5.1 14.1L2.6 13.6L2.6 10.4L5.1 9.9L5.6 8.6L4.2 6.5L6.5 4.2L8.6 5.6L9.9 5.1L10.4 2.6L13.6 2.6L14.1 5.1L15.4 5.6L17.5 4.2L19.8 6.5L18.4 8.6Z",
    "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6",
  ],
  down: ["M12 4v11", "m7 10 5 5 5-5", "M5 20h14"],
  search: ["M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14", "m20 20-3.5-3.5"],
  check: ["m5 12 5 5 9-10"],
  alert: ["M12 3 2 20h20z", "M12 10v4", "M12 17h.01"],
  x: ["m6 6 12 12", "M18 6 6 18"],
  film: ["M4 4h16v16H4z", "M8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4"],
  tv: ["M3 6h18v12H3z", "m8 3 4 3 4-3"],
  refresh: ["M20 11a8 8 0 1 0-2.3 5.7", "M20 4v7h-7"],
  trash: ["M4 7h16", "M10 11v6M14 11v6", "M6 7l1 13h10l1-13", "M9 7V4h6v3"],
  logout: ["M15 4h4v16h-4", "M10 8l-4 4 4 4", "M6 12h11"],
  info: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M12 11v5", "M12 8h.01"],
  dots: ["M6 12h.01M12 12h.01M18 12h.01"],
  skip: ["M5 12h14"],
  clock: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M12 7v5l3 2"],
};

const NS = "http://www.w3.org/2000/svg";

export function icon(name, label) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "i");
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else svg.setAttribute("aria-hidden", "true");
  for (const d of PATHS[name] || PATHS.dots) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
