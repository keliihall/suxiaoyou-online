/** Browser-side audit of rendered app text. No API requests or saved settings.
 * Alpha colors are composited over the actual ancestor surfaces. Images,
 * gradients and filters are reported for manual review, never guessed as passes.
 */
export function auditVisibleContrast({ rootSelector = "body" } = {}) {
  const root = document.querySelector(rootSelector);
  if (!root) throw new Error(`Missing audit root: ${rootSelector}`);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("Color normalization is unavailable");
  const cache = new Map();
  function rgba(color) {
    if (cache.has(color)) return cache.get(color);
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = color;
    context.fillRect(0, 0, 1, 1);
    const p = context.getImageData(0, 0, 1, 1).data;
    const value = [p[0] / 255, p[1] / 255, p[2] / 255, p[3] / 255];
    cache.set(color, value);
    return value;
  }
  function over(top, bottom) {
    const alpha = top[3] + bottom[3] * (1 - top[3]);
    if (!alpha) return [0, 0, 0, 0];
    return [0, 1, 2].map(i => (top[i] * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / alpha).concat(alpha);
  }
  function luminance(color) {
    return color.slice(0, 3).map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  }
  function contrast(fg, bg) {
    const a = luminance(fg), b = luminance(bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  }
  function selector(element) {
    if (element.id) return `#${CSS.escape(element.id)}`;
    const path = [];
    let e = element;
    for (let i = 0; e && e !== root && i < 4; i++, e = e.parentElement) {
      const siblings = e.parentElement ? [...e.parentElement.children].filter(s => s.tagName === e.tagName) : [];
      path.unshift(e.tagName.toLowerCase() + (siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(e) + 1})` : ""));
    }
    return path.join(" > ");
  }
  const failures = [], manualReview = [];
  let checked = 0, minimumRatio = Infinity;
  const seen = new Set();
  for (const element of [root, ...root.querySelectorAll("*")]) {
    if (!(element instanceof HTMLElement) || element.closest("svg,canvas,script,style,[hidden],[inert],[aria-hidden=true]")) continue;
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height || rect.right <= 0 || rect.left >= innerWidth) continue;
    const style = getComputedStyle(element);
    if (style.visibility !== "visible" || style.display === "none") continue;
    const input = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement;
    if (input && element instanceof HTMLInputElement && ["hidden", "checkbox", "radio", "range", "file", "color"].includes(element.type)) continue;
    const ownText = [...element.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    const inputText = input && (element.value || element.getAttribute("placeholder"));
    const select = element instanceof HTMLSelectElement;
    if (!ownText && !inputText && !select) continue;
    if (["OPTION", "OPTGROUP"].includes(element.tagName)) continue;
    const chain = [];
    let opacity = 1, complex = false, invisible = false;
    for (let e = element; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      opacity *= Number(s.opacity);
      if (s.visibility !== "visible" || s.display === "none" || Number(s.opacity) === 0) invisible = true;
      if (s.backgroundImage !== "none" || s.filter !== "none") complex = true;
      chain.push(s);
    }
    if (invisible || opacity < 0.01) continue;
    const disabled = !!element.closest(":disabled,[aria-disabled=true],[data-disabled]");
    const placeholder = input && !element.value && !!element.getAttribute("placeholder");
    const fgColor = placeholder ? getComputedStyle(element, "::placeholder").color : input && disabled ? style.webkitTextFillColor || style.color : style.color;
    let bg = document.documentElement.classList.contains("dark") ? [16 / 255, 22 / 255, 32 / 255, 1] : [1, 1, 1, 1];
    for (const s of chain.reverse()) bg = over(rgba(s.backgroundColor), bg);
    const fg = [...rgba(fgColor)];
    fg[3] *= opacity;
    const paintedFg = over(fg, bg);
    const ratio = contrast(paintedFg, bg);
    const fontSize = Number.parseFloat(style.fontSize);
    const large = fontSize >= 24 || (fontSize >= 18.66 && Number.parseInt(style.fontWeight) >= 700);
    // Disabled controls are exempt from WCAG text contrast; this project still
    // checks a 3:1 readability floor instead of fading entire cards to 50%.
    const minimum = disabled || large ? 3 : 4.5;
    const item = { selector: selector(element), tag: element.tagName.toLowerCase(), classes: String(element.className).slice(0, 300), kind: placeholder ? "placeholder" : disabled ? "disabled" : "text", foreground: fgColor, background: bg.slice(0, 3).map(v => Math.round(v * 255)), opacity, ratio: Math.round(ratio * 100) / 100, minimum };
    const key = [fgColor, item.background.join(","), opacity, fontSize, disabled, item.classes].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    if (complex) { manualReview.push(item); continue; }
    checked++;
    minimumRatio = Math.min(minimumRatio, ratio);
    if (ratio < minimum) failures.push(item);
  }
  return { checked, minimumRatio: Number.isFinite(minimumRatio) ? Math.round(minimumRatio * 100) / 100 : null, failures, manualReview };
}
