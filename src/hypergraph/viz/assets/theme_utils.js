/**
 * Minimal theme utilities shared by HyperNodes visual frontends.
 * Provides host background + light/dark inference without imposing palettes.
 */
(function (window) {
  const ns = (window.HyperGraphTheme = window.HyperGraphTheme || {});

  function parseColorString(value) {
    if (!value) return null;
    try {
      const scratch = document.createElement("div");
      scratch.style.color = value;
      scratch.style.backgroundColor = value;
      scratch.style.display = "none";
      document.body.appendChild(scratch);
      const resolved = getComputedStyle(scratch).color || "";
      scratch.remove();

      const nums = resolved.match(/[\d\.]+/g);
        if (nums && nums.length >= 3) {
          const [r, g, b] = nums.slice(0, 3).map(Number);
          // Check alpha if present
          if (nums.length >= 4) {
             const alpha = Number(nums[3]);
             if (alpha < 0.1) return null;
          }
          const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
          return { r, g, b, luminance, resolved, raw: value };
        }
    } catch (_) {}
    return null;
  }

  function detectHostTheme() {
    const attempts = [];
    const push = (value, source) => {
      if (value && value !== 'transparent' && value !== 'rgba(0, 0, 0, 0)') {
        attempts.push({ value: String(value).trim(), source });
      }
    };

    // Detect host environment first
    let hostEnv = 'unknown';
    try {
      const parentDoc = window.parent?.document;
      if (parentDoc) {
        // Check for VS Code
        if (parentDoc.body.getAttribute('data-vscode-theme-kind') || 
            (parentDoc.body.className && parentDoc.body.className.includes('vscode'))) {
          hostEnv = 'vscode';
        }
        // Check for JupyterLab
        else if (parentDoc.body.dataset.jpThemeLight !== undefined || 
                 parentDoc.querySelector('.jp-Notebook')) {
          hostEnv = 'jupyterlab';
        }
        // Check for Marimo
        else if (parentDoc.body.dataset.theme || parentDoc.body.dataset.mode ||
                 (parentDoc.body.className && parentDoc.body.className.includes('marimo'))) {
          hostEnv = 'marimo';
        }
      }
    } catch (_) {}

    // Parent document (e.g., VS Code or JupyterLab iframe host)
    try {
      const parentDoc = window.parent?.document;
      if (parentDoc) {
        const rootStyle = getComputedStyle(parentDoc.documentElement);
        const bodyStyle = getComputedStyle(parentDoc.body);
        
        if (hostEnv === 'vscode') {
          // VS Code: use CSS variable
          push(rootStyle.getPropertyValue("--vscode-editor-background"), "parent --vscode-editor-background");
        } else if (hostEnv === 'jupyterlab') {
          // JupyterLab: .jp-Notebook has the actual visible background
          const jpNotebook = parentDoc.querySelector('.jp-Notebook');
          if (jpNotebook) {
            const jpNotebookBg = getComputedStyle(jpNotebook).backgroundColor;
            push(jpNotebookBg, '.jp-Notebook background');
          }
          // JupyterLab CSS variables (fallback)
          push(rootStyle.getPropertyValue("--jp-layout-color0"), "parent --jp-layout-color0");
          push(rootStyle.getPropertyValue("--jp-layout-color1"), "parent --jp-layout-color1");
        } else {
          // Unknown/Marimo: try common sources
          push(rootStyle.getPropertyValue("--vscode-editor-background"), "parent --vscode-editor-background");
          push(rootStyle.getPropertyValue("--jp-layout-color0"), "parent --jp-layout-color0");
        }
        
        // Fallback to computed backgrounds
        push(bodyStyle.backgroundColor, "parent body background");
        push(rootStyle.backgroundColor, "parent root background");
      }
    } catch (_) {}

    // Current document
    try {
      const rootStyle = getComputedStyle(document.documentElement);
      const bodyStyle = getComputedStyle(document.body);
      push(rootStyle.getPropertyValue("--vscode-editor-background"), "--vscode-editor-background");
      push(bodyStyle.backgroundColor, "body background");
      push(rootStyle.backgroundColor, "root background");
    } catch (_) {}

    // Choose first parsable candidate
    let chosen = attempts.find((c) => parseColorString(c.value));
    if (!chosen && attempts.length) chosen = attempts[0];

    const parsed = chosen ? parseColorString(chosen.value) : null;
    let theme = parsed && typeof parsed.luminance === "number"
      ? parsed.luminance > 150
        ? "light"
        : "dark"
      : null;
    let source = chosen?.source || "fallback";

    // JupyterLab explicit attributes (check before VS Code)
    try {
      const parentDoc = window.parent?.document;
      if (parentDoc) {
        // JupyterLab uses data-jp-theme-light attribute ("true" or "false")
        const jpThemeLight = parentDoc.body.dataset.jpThemeLight;
        if (jpThemeLight === "true") {
          theme = "light";
          source = "jupyterlab data-jp-theme-light";
        } else if (jpThemeLight === "false") {
          theme = "dark";
          source = "jupyterlab data-jp-theme-light";
        }
        // JupyterLab body classes
        const bodyClass = parentDoc.body.className || "";
        if (!theme && bodyClass.includes("jp-mod-dark")) {
          theme = "dark";
          source = "jupyterlab jp-mod-dark";
        } else if (!theme && bodyClass.includes("jp-mod-light")) {
          theme = "light";
          source = "jupyterlab jp-mod-light";
        }
      }
    } catch (_) {}

    // VS Code explicit attributes
    try {
      const docForAttrs = window.parent?.document || document;
      const themeKind = docForAttrs?.body?.getAttribute("data-vscode-theme-kind") || "";
      const bodyClass = docForAttrs?.body?.className || "";
      if (themeKind.includes("light") || bodyClass.includes("vscode-light")) {
        theme = "light";
        source = "vscode attribute";
      } else if (themeKind.includes("dark") || bodyClass.includes("vscode-dark")) {
        theme = "dark";
        source = "vscode attribute";
      }
    } catch (_) {}

    // Marimo notebook detection
    try {
      const parentDoc = window.parent?.document;
      if (parentDoc && !theme) {
        // Marimo uses data-theme or data-mode attributes
        const dataTheme = parentDoc.body.dataset.theme || parentDoc.documentElement.dataset.theme;
        const dataMode = parentDoc.body.dataset.mode || parentDoc.documentElement.dataset.mode;
        if (dataTheme === "dark" || dataMode === "dark") {
          theme = "dark";
          source = "marimo data-theme/mode";
        } else if (dataTheme === "light" || dataMode === "light") {
          theme = "light";
          source = "marimo data-theme/mode";
        }
        // Marimo body classes
        const bodyClass = parentDoc.body.className || "";
        if (!theme && (bodyClass.includes("dark-mode") || bodyClass.includes("dark"))) {
          theme = "dark";
          source = "marimo dark-mode class";
        }
        // Check color-scheme CSS property
        if (!theme) {
          const colorScheme = getComputedStyle(parentDoc.documentElement).getPropertyValue("color-scheme").trim();
          if (colorScheme.includes("dark")) {
            theme = "dark";
            source = "color-scheme property";
          } else if (colorScheme.includes("light")) {
            theme = "light";
            source = "color-scheme property";
          }
        }
      }
    } catch (_) {}

    // Fallback to prefers-color-scheme
    if (!theme && window.matchMedia) {
      if (window.matchMedia("(prefers-color-scheme: light)").matches) {
        theme = "light";
        source = "prefers-color-scheme";
      } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
        theme = "dark";
        source = "prefers-color-scheme";
      }
    }

    return {
      theme: theme || "dark",
      background: parsed?.resolved || parsed?.raw || chosen?.value || "transparent",
      luminance: parsed?.luminance ?? null,
      source,
    };
  }

  ns.parseColorString = parseColorString;
  ns.detectHostTheme = detectHostTheme;
})(window);
