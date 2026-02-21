/**
 * Slider panel for tuning all numeric constants from constants.js.
 *
 * Constants are grouped by category. Each slider shows name, current value,
 * and min/max range. Changes propagate up via onChange callback.
 */
import React, { useState, useCallback } from 'react';

// [default, min, max, step]
// Only the tunable constant — everything else is fixed
const CONSTANT_GROUPS = {
  'Edge Shape': {
    EDGE_ELBOW_RADIUS:              [28, 0, 60, 1],
    EDGE_TARGET_INSET:              [12, 0, 40, 1],
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
