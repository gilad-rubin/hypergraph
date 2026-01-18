/**
 * Theme detection utilities for hypergraph visualization.
 *
 * Detects the host environment (VSCode, JupyterLab, Marimo) and its theme.
 * Uses host-first detection: always identify the environment before reading colors.
 */

/**
 * Detect host environment and theme.
 *
 * @returns {{host: string, theme: 'light'|'dark'}}
 */
function detectTheme() {
    // Try to access parent document (we're in an iframe)
    let parentDoc;
    try {
        parentDoc = window.parent?.document || document;
    } catch (e) {
        // Cross-origin access denied
        parentDoc = document;
    }

    // 1. Detect host environment
    let host = "unknown";

    // VSCode
    const vscodeThemeKind = parentDoc.body?.getAttribute("data-vscode-theme-kind");
    if (vscodeThemeKind) {
        host = "vscode";
    }
    // JupyterLab
    else if (parentDoc.body?.dataset?.jpThemeLight !== undefined) {
        host = "jupyterlab";
    }
    // Marimo
    else if (parentDoc.body?.className?.includes("marimo")) {
        host = "marimo";
    }

    // 2. Detect theme based on host
    let theme = "dark"; // Default

    switch (host) {
        case "vscode":
            theme = detectVSCodeTheme(vscodeThemeKind);
            break;
        case "jupyterlab":
            theme = detectJupyterTheme(parentDoc);
            break;
        case "marimo":
            theme = detectMarimoTheme(parentDoc);
            break;
        default:
            theme = detectFallbackTheme(parentDoc);
    }

    return { host, theme };
}

/**
 * Detect VSCode theme from data attribute.
 *
 * @param {string} themeKind - Value of data-vscode-theme-kind
 * @returns {'light'|'dark'}
 */
function detectVSCodeTheme(themeKind) {
    // Values: 'vscode-dark', 'vscode-light', 'vscode-high-contrast', 'vscode-high-contrast-light'
    if (themeKind?.includes("light")) {
        return "light";
    }
    return "dark";
}

/**
 * Detect JupyterLab theme.
 *
 * @param {Document} doc - Parent document
 * @returns {'light'|'dark'}
 */
function detectJupyterTheme(doc) {
    // Best: Check data attribute directly
    const isLight = doc.body?.dataset?.jpThemeLight;
    if (isLight !== undefined) {
        return isLight === "true" ? "light" : "dark";
    }

    // Fallback: Read .jp-Notebook background
    const notebook = doc.querySelector(".jp-Notebook");
    if (notebook) {
        return getThemeFromBackground(notebook);
    }

    return "dark";
}

/**
 * Detect Marimo theme.
 *
 * @param {Document} doc - Parent document
 * @returns {'light'|'dark'}
 */
function detectMarimoTheme(doc) {
    const dataTheme = doc.body?.dataset?.theme || doc.body?.dataset?.mode;
    return dataTheme === "light" ? "light" : "dark";
}

/**
 * Fallback theme detection using background luminance.
 *
 * @param {Document} doc - Parent document
 * @returns {'light'|'dark'}
 */
function detectFallbackTheme(doc) {
    if (doc.body) {
        return getThemeFromBackground(doc.body);
    }
    return "dark";
}

/**
 * Determine theme from element's background color luminance.
 *
 * @param {Element} element - DOM element
 * @returns {'light'|'dark'}
 */
function getThemeFromBackground(element) {
    try {
        const style = window.parent?.getComputedStyle(element) || getComputedStyle(element);
        const bg = style.backgroundColor;

        // Parse RGB from "rgb(r, g, b)" or "rgba(r, g, b, a)"
        const match = bg.match(/[\d.]+/g);
        if (!match || match.length < 3) {
            return "dark";
        }

        const [r, g, b] = match.slice(0, 3).map(Number);

        // Calculate perceived luminance
        // Formula: 0.299*R + 0.587*G + 0.114*B
        const luminance = 0.299 * r + 0.587 * g + 0.114 * b;

        return luminance > 128 ? "light" : "dark";
    } catch (e) {
        return "dark";
    }
}

/**
 * Parse a color string that might be a named color.
 * JupyterLab CSS variables can return "white" instead of "rgb(255, 255, 255)".
 *
 * @param {string} value - Color value (e.g., "white", "rgb(0,0,0)")
 * @returns {string} - Color in "rgb(r, g, b)" format
 */
function parseColorString(value) {
    if (!value) return "rgb(0, 0, 0)";

    // If already in rgb format, return as-is
    if (value.startsWith("rgb")) {
        return value;
    }

    // Create scratch element to resolve named colors
    try {
        const scratch = document.createElement("div");
        scratch.style.backgroundColor = value;
        document.body.appendChild(scratch);
        const resolved = getComputedStyle(scratch).backgroundColor;
        scratch.remove();
        return resolved;
    } catch (e) {
        return "rgb(0, 0, 0)";
    }
}

// Export for use in other scripts
window.detectTheme = detectTheme;
window.HypergraphThemeUtils = {
    detectTheme,
    detectVSCodeTheme,
    detectJupyterTheme,
    detectMarimoTheme,
    getThemeFromBackground,
    parseColorString
};
