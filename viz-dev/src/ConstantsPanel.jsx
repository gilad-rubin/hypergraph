/**
 * Slider panel for tuning all numeric constants from constants.js.
 *
 * Constants are grouped by category. Each slider shows name, current value,
 * and min/max range. Changes propagate up via onChange callback.
 */
import React, { useState, useCallback } from 'react';

// [default, min, max, step]
// Grouped by the 4 layout rules: Centering, Convergence, Routing, Layout
const CONSTANT_GROUPS = {
  'Centering': {
    BRANCH_CENTER_WEIGHT:           [1, 0, 1, 0.05],
    FAN_CENTER_WEIGHT:              [0.8, 0, 1, 0.05],
    INPUT_FAN_CENTER_WEIGHT:        [0.7, 0, 1, 0.05],
    DATA_NODE_ALIGN_WEIGHT:         [1, 0, 1, 0.05],
    INPUT_NODE_ALIGN_WEIGHT:        [0.9, 0, 1, 0.05],
    EDGE_SHARED_TARGET_SPACING_SCALE: [0.5, 0, 2, 0.05],
  },
  'Convergence': {
    EDGE_CONVERGENCE_OFFSET:        [15, 0, 60, 1],
    EDGE_SOURCE_DIVERGE_OFFSET:     [20, 0, 60, 1],
  },
  'Routing': {
    EDGE_STRAIGHTEN_MAX_SHIFT:      [0, 0, 400, 5],
    EDGE_MICRO_X_SNAP:              [20, 0, 40, 1],
    EDGE_ANGLE_WEIGHT:              [0.1, 0, 10, 0.1],
    EDGE_CURVE_WEIGHT:              [0.5, 0, 20, 0.5],
    EDGE_NODE_PENALTY:              [0, 0, 100, 1],
    EDGE_NODE_CLEARANCE:            [0, 0, 40, 1],
  },
  'Layout': {
    VERTICAL_GAP:                   [95, 40, 200, 5],
    GRAPH_PADDING:                  [26, 8, 48, 2],
    HEADER_HEIGHT:                  [20, 10, 60, 2],
  },
  'Edge Shape': {
    EDGE_CURVE_STYLE:               [0, 0, 1, 0.01],
    EDGE_ELBOW_RADIUS:              [28, 0, 60, 1],
    EDGE_MICRO_MERGE_ANGLE:         [60, 0, 90, 1],
    EDGE_TURN_SOFTENING:            [0, 0, 0.5, 0.01],
    EDGE_SHARP_TURN_ANGLE:          [0, 0, 90, 1],
  },
  'Feedback Edges': {
    FEEDBACK_EDGE_GUTTER:           [65, 20, 150, 5],
    FEEDBACK_EDGE_HEADROOM:         [100, 10, 200, 5],
    FEEDBACK_EDGE_STEM:             [32, 10, 80, 2],
    FEEDBACK_EDGE_STUB:             [24, 8, 60, 2],
  },
  'Node Sizing': {
    NODE_BASE_PADDING:              [60, 20, 100, 2],
    FUNCTION_NODE_BASE_PADDING:     [54, 20, 100, 2],
    MAX_NODE_WIDTH:                 [340, 150, 500, 10],
    TYPE_HINT_MAX_CHARS:            [27, 10, 50, 1],
    NODE_LABEL_MAX_CHARS:           [27, 10, 50, 1],
    CHAR_WIDTH_PX:                  [7, 4, 12, 0.5],
  },
};

// Build flat defaults map
const DEFAULTS = {};
for (const group of Object.values(CONSTANT_GROUPS)) {
  for (const [key, [defaultVal]] of Object.entries(group)) {
    DEFAULTS[key] = defaultVal;
  }
}

function Slider({ name, value, min, max, step, onChange, isModified }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
      <label
        style={{
          flex: '0 0 220px',
          fontSize: 11,
          fontFamily: 'monospace',
          color: isModified ? '#facc15' : '#94a3b8',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
        title={name}
      >
        {name}
      </label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={e => onChange(name, parseFloat(e.target.value))}
        style={{ flex: 1, accentColor: '#3b82f6', height: 14 }}
      />
      <span
        style={{
          flex: '0 0 50px',
          fontSize: 11,
          fontFamily: 'monospace',
          textAlign: 'right',
          color: isModified ? '#facc15' : '#e5e7eb',
        }}
      >
        {Number.isInteger(step) ? value : value.toFixed(String(step).split('.')[1]?.length || 2)}
      </span>
    </div>
  );
}

export default function ConstantsPanel({ onChange }) {
  const [values, setValues] = useState(() => {
    // Initialize from current window constants (may differ from defaults if already tuned)
    const current = { ...DEFAULTS };
    const live = window.HypergraphVizConstants;
    if (live) {
      for (const key of Object.keys(current)) {
        if (key in live && typeof live[key] === 'number') {
          current[key] = live[key];
        }
      }
    }
    return current;
  });

  const [collapsedGroups, setCollapsedGroups] = useState({});

  const handleChange = useCallback((name, newValue) => {
    setValues(prev => {
      const next = { ...prev, [name]: newValue };
      onChange(next);
      return next;
    });
  }, [onChange]);

  const handleReset = useCallback(() => {
    setValues({ ...DEFAULTS });
    onChange({ ...DEFAULTS });
  }, [onChange]);

  const toggleGroup = useCallback((groupName) => {
    setCollapsedGroups(prev => ({
      ...prev,
      [groupName]: !prev[groupName],
    }));
  }, []);

  const modifiedCount = Object.entries(values).filter(
    ([k, v]) => DEFAULTS[k] !== undefined && v !== DEFAULTS[k]
  ).length;

  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        marginBottom: 8, paddingBottom: 6, borderBottom: '1px solid #334155',
      }}>
        <span style={{ fontSize: 11, color: '#94a3b8' }}>
          {modifiedCount > 0 ? `${modifiedCount} modified` : 'All defaults'}
        </span>
        <button
          onClick={handleReset}
          style={{
            fontSize: 10, padding: '2px 8px', background: '#1e293b',
            border: '1px solid #334155', borderRadius: 4, color: '#94a3b8',
            cursor: 'pointer',
          }}
        >
          Reset All
        </button>
      </div>

      {Object.entries(CONSTANT_GROUPS).map(([groupName, constants]) => {
        const isCollapsed = collapsedGroups[groupName];
        const groupModified = Object.keys(constants).filter(
          k => values[k] !== DEFAULTS[k]
        ).length;

        return (
          <div key={groupName} style={{ marginBottom: 8 }}>
            <div
              onClick={() => toggleGroup(groupName)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                cursor: 'pointer', padding: '3px 0', userSelect: 'none',
              }}
            >
              <span style={{ fontSize: 9, color: '#64748b' }}>
                {isCollapsed ? '\u25B6' : '\u25BC'}
              </span>
              <span style={{
                fontSize: 11, fontWeight: 600, color: '#e5e7eb',
                textTransform: 'uppercase', letterSpacing: '0.05em',
              }}>
                {groupName}
              </span>
              {groupModified > 0 && (
                <span style={{
                  fontSize: 9, color: '#facc15', background: '#422006',
                  padding: '0 4px', borderRadius: 3,
                }}>
                  {groupModified}
                </span>
              )}
            </div>
            {!isCollapsed && Object.entries(constants).map(([name, [defaultVal, min, max, step]]) => (
              <Slider
                key={name}
                name={name}
                value={values[name]}
                min={min}
                max={max}
                step={step}
                onChange={handleChange}
                isModified={values[name] !== defaultVal}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}

export { DEFAULTS };
