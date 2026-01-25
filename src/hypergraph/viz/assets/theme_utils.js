/**
 * Theme detection utilities for Hypergraph visualization
 * Detects host environment theme (VS Code, JupyterLab, Marimo, etc.)
 */
(function(root, factory) {
  var api = factory();
  if (root) root.HypergraphVizTheme = api;
})(typeof window !== 'undefined' ? window : this, function() {
  'use strict';

  /**
   * Parse a color string and extract RGB values + luminance
   * @param {string} value - CSS color value
   * @returns {Object|null} { r, g, b, luminance, resolved, raw } or null if invalid
   */
  function parseColorString(value) {
    if (!value) return null;
    var scratch = document.createElement('div');
    scratch.style.color = value;
    scratch.style.backgroundColor = value;
    scratch.style.display = 'none';
    document.body.appendChild(scratch);
    var resolved = getComputedStyle(scratch).color || '';
    scratch.remove();
    var nums = resolved.match(/[\d\.]+/g);
    if (nums && nums.length >= 3) {
      var r = Number(nums[0]);
      var g = Number(nums[1]);
      var b = Number(nums[2]);
      if (nums.length >= 4) {
        var alpha = Number(nums[3]);
        if (alpha < 0.1) return null;
      }
      var luminance = 0.299 * r + 0.587 * g + 0.114 * b;
      return { r: r, g: g, b: b, luminance: luminance, resolved: resolved, raw: value };
    }
    return null;
  }

  /**
   * Detect the host environment's theme
   * Checks for VS Code, JupyterLab, Marimo, and general system preferences
   * @returns {Object} { theme: 'light'|'dark', background: string, luminance: number|null, source: string }
   */
  function detectHostTheme() {
    var attempts = [];
    var pushCandidate = function(value, source) {
      if (value && value !== 'transparent' && value !== 'rgba(0, 0, 0, 0)') {
        attempts.push({ value: value.trim(), source: source });
      }
    };

    // Detect host environment first
    var hostEnv = 'unknown';
    try {
      var parentDoc = window.parent && window.parent.document;
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
    } catch (e) {}

    try {
      parentDoc = window.parent && window.parent.document;
      if (parentDoc) {
        var rootStyle = getComputedStyle(parentDoc.documentElement);
        var bodyStyle = getComputedStyle(parentDoc.body);

        if (hostEnv === 'vscode') {
          // VS Code: use CSS variable
          pushCandidate(rootStyle.getPropertyValue('--vscode-editor-background'), '--vscode-editor-background');
        } else if (hostEnv === 'jupyterlab') {
          // JupyterLab: .jp-Notebook has the actual visible background
          var jpNotebook = parentDoc.querySelector('.jp-Notebook');
          if (jpNotebook) {
            var jpNotebookBg = getComputedStyle(jpNotebook).backgroundColor;
            pushCandidate(jpNotebookBg, '.jp-Notebook background');
          }
          // JupyterLab CSS variables (fallback)
          pushCandidate(rootStyle.getPropertyValue('--jp-layout-color0'), '--jp-layout-color0');
          pushCandidate(rootStyle.getPropertyValue('--jp-layout-color1'), '--jp-layout-color1');
        } else {
          // Unknown/Marimo: try common sources
          pushCandidate(rootStyle.getPropertyValue('--vscode-editor-background'), '--vscode-editor-background');
          pushCandidate(rootStyle.getPropertyValue('--jp-layout-color0'), '--jp-layout-color0');
        }

        // Fallback to computed backgrounds
        pushCandidate(bodyStyle.backgroundColor, 'parent body background');
        pushCandidate(rootStyle.backgroundColor, 'parent root background');
      }
    } catch (e) {}

    pushCandidate(getComputedStyle(document.body).backgroundColor, 'iframe body');

    var chosen = attempts.find(function(c) { return parseColorString(c.value); });
    if (!chosen) chosen = { value: 'transparent', source: 'default' };
    var parsed = parseColorString(chosen.value);
    var luminance = parsed ? parsed.luminance : null;

    var autoTheme = luminance !== null ? (luminance > 150 ? 'light' : 'dark') : null;
    var source = luminance !== null ? (chosen.source + ' luminance') : chosen.source;

    // JupyterLab detection (check before VS Code)
    try {
      parentDoc = window.parent && window.parent.document;
      if (parentDoc) {
        // JupyterLab uses data-jp-theme-light attribute ("true" or "false")
        var jpThemeLight = parentDoc.body.dataset.jpThemeLight;
        if (jpThemeLight === 'true') {
          autoTheme = 'light';
          source = 'jupyterlab data-jp-theme-light';
        } else if (jpThemeLight === 'false') {
          autoTheme = 'dark';
          source = 'jupyterlab data-jp-theme-light';
        }
        // JupyterLab body classes
        var bodyClass = parentDoc.body.className || '';
        if (!autoTheme && bodyClass.includes('jp-mod-dark')) {
          autoTheme = 'dark';
          source = 'jupyterlab jp-mod-dark';
        } else if (!autoTheme && bodyClass.includes('jp-mod-light')) {
          autoTheme = 'light';
          source = 'jupyterlab jp-mod-light';
        }
      }
    } catch (e) {}

    // VS Code detection
    try {
      parentDoc = window.parent && window.parent.document;
      if (parentDoc) {
        var themeKind = parentDoc.body.getAttribute('data-vscode-theme-kind');
        if (themeKind) {
          autoTheme = themeKind.includes('light') ? 'light' : 'dark';
          source = 'vscode-theme-kind';
        } else if (parentDoc.body.className && parentDoc.body.className.includes('vscode-light')) {
          autoTheme = 'light';
          source = 'vscode body class';
        } else if (parentDoc.body.className && parentDoc.body.className.includes('vscode-dark')) {
          autoTheme = 'dark';
          source = 'vscode body class';
        }
      }
    } catch (e) {}

    // Marimo detection
    try {
      parentDoc = window.parent && window.parent.document;
      if (parentDoc && !autoTheme) {
        // Marimo uses data-theme or data-mode attributes
        var dataTheme = parentDoc.body.dataset.theme || parentDoc.documentElement.dataset.theme;
        var dataMode = parentDoc.body.dataset.mode || parentDoc.documentElement.dataset.mode;
        if (dataTheme === 'dark' || dataMode === 'dark') {
          autoTheme = 'dark';
          source = 'marimo data-theme/mode';
        } else if (dataTheme === 'light' || dataMode === 'light') {
          autoTheme = 'light';
          source = 'marimo data-theme/mode';
        }
        // Marimo body classes
        bodyClass = parentDoc.body.className || '';
        if (!autoTheme && (bodyClass.includes('dark-mode') || bodyClass.includes('dark'))) {
          autoTheme = 'dark';
          source = 'marimo dark-mode class';
        }
        // Check color-scheme CSS property
        if (!autoTheme) {
          var colorScheme = getComputedStyle(parentDoc.documentElement).getPropertyValue('color-scheme').trim();
          if (colorScheme.includes('dark')) {
            autoTheme = 'dark';
            source = 'color-scheme property';
          } else if (colorScheme.includes('light')) {
            autoTheme = 'light';
            source = 'color-scheme property';
          }
        }
      }
    } catch (e) {}

    // Fallback to prefers-color-scheme
    if (!autoTheme && window.matchMedia) {
      if (window.matchMedia('(prefers-color-scheme: light)').matches) {
        autoTheme = 'light';
        source = 'prefers-color-scheme';
      } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
        autoTheme = 'dark';
        source = 'prefers-color-scheme';
      }
    }

    return {
      theme: autoTheme || 'dark',
      background: parsed ? (parsed.resolved || parsed.raw || chosen.value) : chosen.value,
      luminance: luminance,
      source: source,
    };
  }

  /**
   * Normalize theme preference string
   * @param {string} pref - User preference ('light', 'dark', 'auto', or other)
   * @returns {string} 'light', 'dark', or 'auto'
   */
  function normalizeThemePref(pref) {
    var lower = (pref || '').toLowerCase();
    return ['light', 'dark', 'auto'].includes(lower) ? lower : 'auto';
  }

  // Export API
  return {
    parseColorString: parseColorString,
    detectHostTheme: detectHostTheme,
    normalizeThemePref: normalizeThemePref
  };
});
