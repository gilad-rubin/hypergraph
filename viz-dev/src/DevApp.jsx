/**
 * DevApp — orchestrator for the viz dev environment.
 *
 * Manages graph selection, constant tuning, and script reloading.
 * Mounts as a floating panel over the viz canvas.
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { loadManifest, loadGraphData } from './graph-loader';
import ConstantsPanel, { DEFAULTS } from './ConstantsPanel';
import { reloadDependentScripts } from './main';

const DEBOUNCE_MS = 150;

export default function DevApp() {
  const [manifest, setManifest] = useState(null);
  const [selectedGraph, setSelectedGraph] = useState(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [status, setStatus] = useState('Loading...');
  const [error, setError] = useState(null);
  const debounceRef = useRef(null);
  const currentConstantsRef = useRef({ ...DEFAULTS });

  // Load manifest on mount
  useEffect(() => {
    loadManifest()
      .then(m => {
        setManifest(m);
        if (m.graphs?.length > 0) {
          setSelectedGraph(m.graphs[0].id);
        } else {
          setStatus('No graphs in manifest. Run: npm run generate');
        }
      })
      .catch(err => {
        setError(`Manifest load failed: ${err.message}`);
        setStatus('Run: npm run generate');
      });
  }, []);

  /**
   * Re-init the viz: replace the #root element, reload dependent scripts,
   * then call init(). Replacing the DOM element avoids React's "already
   * has a root" warning.
   */
  const reinitViz = useCallback(async () => {
    setStatus('Re-rendering...');
    setError(null);
    try {
      // Replace #root element to get a clean React mount point
      const oldRoot = document.getElementById('root');
      const newRoot = oldRoot.cloneNode(false);
      newRoot.id = 'root';
      newRoot.innerHTML = '<div id="fallback">Re-rendering...</div>';
      oldRoot.parentNode.replaceChild(newRoot, oldRoot);

      // Reload scripts that capture constants at load time
      await reloadDependentScripts();

      // Re-init the viz app
      window.HypergraphVizApp.init();
      setStatus('Ready');
    } catch (err) {
      console.error('[viz-dev] Re-init failed:', err);
      setError(err.message);
      setStatus('Error');
    }
  }, []);

  // Load graph data when selection changes
  useEffect(() => {
    if (!selectedGraph) return;

    let cancelled = false;
    setStatus('Loading graph...');

    loadGraphData(selectedGraph)
      .then(data => {
        if (cancelled) return;

        // Inject graph data into the script tag (same mechanism as production)
        const el = document.getElementById('graph-data');
        el.textContent = JSON.stringify(data);

        // Apply current constants before re-init
        Object.assign(window.HypergraphVizConstants, currentConstantsRef.current);

        return reinitViz();
      })
      .catch(err => {
        if (!cancelled) {
          setError(`Graph load failed: ${err.message}`);
          setStatus('Error');
        }
      });

    return () => { cancelled = true; };
  }, [selectedGraph, reinitViz]);

  // Handle constant changes with debounce
  const handleConstantsChange = useCallback((newValues) => {
    currentConstantsRef.current = newValues;

    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      // Mutate the live constants object
      Object.assign(window.HypergraphVizConstants, newValues);
      reinitViz();
    }, DEBOUNCE_MS);
  }, [reinitViz]);

  // Copy current constants as JSON
  const handleCopyConstants = useCallback(() => {
    const live = window.HypergraphVizConstants;
    if (!live) return;

    // Extract only numeric constants (skip objects like NODE_TYPE_OFFSETS)
    const exported = {};
    for (const [key, value] of Object.entries(live)) {
      if (typeof value === 'number') {
        exported[key] = value;
      }
    }

    navigator.clipboard.writeText(JSON.stringify(exported, null, 2))
      .then(() => setStatus('Copied!'))
      .catch(() => setStatus('Copy failed'));

    setTimeout(() => setStatus('Ready'), 1500);
  }, []);

  return (
    <div style={{
      position: 'fixed',
      top: 0,
      right: 0,
      height: '100vh',
      width: panelOpen ? 360 : 40,
      zIndex: 100,
      display: 'flex',
      flexDirection: 'column',
      transition: 'width 0.15s ease',
      pointerEvents: 'none',
    }}>
      {/* Toggle button */}
      <button
        onClick={() => setPanelOpen(p => !p)}
        style={{
          position: 'absolute',
          top: 8,
          left: panelOpen ? -32 : 4,
          width: 28,
          height: 28,
          background: '#0f172a',
          border: '1px solid #334155',
          borderRadius: 6,
          color: '#94a3b8',
          fontSize: 14,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          pointerEvents: 'auto',
          zIndex: 101,
        }}
        title={panelOpen ? 'Hide panel' : 'Show panel'}
      >
        {panelOpen ? '\u25B6' : '\u25C0'}
      </button>

      {panelOpen && (
        <div style={{
          flex: 1,
          background: '#0f172aee',
          borderLeft: '1px solid #1e293b',
          backdropFilter: 'blur(12px)',
          display: 'flex',
          flexDirection: 'column',
          pointerEvents: 'auto',
          overflow: 'hidden',
        }}>
          {/* Header */}
          <div style={{
            padding: '10px 12px 8px',
            borderBottom: '1px solid #1e293b',
          }}>
            <div style={{
              fontSize: 12, fontWeight: 700, color: '#e5e7eb',
              letterSpacing: '0.03em', marginBottom: 6,
            }}>
              Viz Dev
            </div>

            {/* Graph selector */}
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
              <select
                value={selectedGraph || ''}
                onChange={e => setSelectedGraph(e.target.value)}
                style={{
                  flex: 1, fontSize: 11, padding: '4px 6px',
                  background: '#1e293b', border: '1px solid #334155',
                  borderRadius: 4, color: '#e5e7eb',
                }}
              >
                {!manifest && <option value="">Loading...</option>}
                {manifest?.graphs?.map(g => (
                  <option key={g.id} value={g.id}>{g.name}</option>
                ))}
              </select>
            </div>

            {/* Action buttons */}
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                onClick={reinitViz}
                style={{
                  flex: 1, fontSize: 10, padding: '4px 8px',
                  background: '#1e293b', border: '1px solid #334155',
                  borderRadius: 4, color: '#94a3b8', cursor: 'pointer',
                }}
              >
                Re-layout
              </button>
              <button
                onClick={handleCopyConstants}
                style={{
                  flex: 1, fontSize: 10, padding: '4px 8px',
                  background: '#1e293b', border: '1px solid #334155',
                  borderRadius: 4, color: '#94a3b8', cursor: 'pointer',
                }}
              >
                Copy Constants
              </button>
            </div>

            {/* Status */}
            <div style={{
              fontSize: 10, marginTop: 4,
              color: error ? '#f87171' : '#64748b',
              fontFamily: 'monospace',
            }}>
              {error || status}
            </div>
          </div>

          {/* Constants panel (scrollable) */}
          <div style={{
            flex: 1,
            overflowY: 'auto',
            padding: '8px 12px',
          }}>
            <ConstantsPanel onChange={handleConstantsChange} />
          </div>
        </div>
      )}
    </div>
  );
}
