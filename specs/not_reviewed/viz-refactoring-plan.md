# HyperNodes Visualization Refactoring Plan

## Executive Summary

Refactor the viz/js implementation to enable:
1. **Full programmatic control** - Expand/collapse nodes, press buttons, query state via API
2. **Storybook + Chromatic** - Isolated component testing with automated visual regression
3. **Playwright E2E testing** - Full integration tests, accessibility checks
4. **Modern JS architecture** - Separate files, TypeScript, proper build tooling
5. **Better debugging** - Source maps, coordinates inspection, state introspection

### Why Storybook + Chromatic?

For AI-assisted development, you need:
- **Fast failing** - Catch visual regressions before they hit main
- **Clear testing** - See exactly what changed in a visual diff
- **No manual clicking** - Automated screenshot comparison on every PR
- **Component isolation** - Test each node type, edge type, state independently

Chromatic provides:
- Cloud-based visual regression testing
- Automatic screenshot capture on every commit
- Visual diff UI showing exactly what pixels changed
- PR integration with GitHub (blocks merge if regressions detected)
- Interaction testing (click, hover, expand states)

---

## Current State Analysis

### Pain Points

| Issue | Impact | Location |
|-------|--------|----------|
| Monolithic JS in Python string | Can't debug, no IDE support, no source maps | `html_generator.py` (2,103 lines) |
| Duplicated state logic | Manual sync required, divergence risk | `state_utils.js` + `state_simulator.py` |
| No programmatic control API | Can't automate testing of UI interactions | N/A |
| No JS build tooling | No TypeScript, no linting, no tree-shaking | N/A |
| Vendor libs bundled inline | ~1.8MB uncompressed, no updates | `assets/*.js` |

### Current File Sizes

| File | Lines | Purpose |
|------|-------|---------|
| `js/html_generator.py` | 2,103 | HTML + embedded React app |
| `assets/state_utils.js` | 912 | Client-side state transformations |
| `state_simulator.py` | 728 | Python port of state_utils.js |
| `assets/elk.bundled.js` | ~45K | ELK layout engine |

---

## Proposed Architecture

```
src/hypernodes/viz/
├── js/
│   ├── src/                         # TypeScript source
│   │   ├── components/
│   │   │   ├── CustomNode.tsx
│   │   │   ├── CustomNode.stories.tsx    # Storybook stories
│   │   │   ├── CustomEdge.tsx
│   │   │   ├── CustomEdge.stories.tsx
│   │   │   ├── DebugOverlay.tsx
│   │   │   ├── Controls.tsx
│   │   │   ├── Controls.stories.tsx
│   │   │   └── OutputsSection.tsx
│   │   ├── hooks/
│   │   │   └── useLayout.ts
│   │   ├── state/
│   │   │   ├── applyState.ts
│   │   │   ├── applyVisibility.ts
│   │   │   ├── compressEdges.ts
│   │   │   └── groupInputs.ts
│   │   ├── api/                     # Programmatic control
│   │   │   ├── vizController.ts
│   │   │   ├── types.ts
│   │   │   └── events.ts
│   │   ├── debug/
│   │   │   ├── inspector.ts
│   │   │   └── validation.ts
│   │   ├── utils/
│   │   │   ├── theme.ts
│   │   │   ├── truncation.ts
│   │   │   └── constants.ts
│   │   ├── App.tsx
│   │   └── index.ts
│   ├── .storybook/                  # Storybook config
│   │   ├── main.ts
│   │   ├── preview.ts
│   │   └── chromatic.config.json
│   ├── dist/                        # Built bundle
│   │   └── hypernodes-viz.umd.js
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
├── assets/
│   ├── vendor/                      # External libs
│   └── tailwind.min.css
├── html_generator.py                # Simplified (~100 lines)
└── ...
```

---

## Phase 1: Programmatic Control API

### Goal
Enable Playwright-based testing with full programmatic control over the visualization.

### VizController Interface

```typescript
interface VizController {
  // State queries
  getExpansionState(): Record<string, boolean>;
  getNodeById(id: string): NodeInfo | null;
  getAllNodes(): NodeInfo[];
  getVisibleNodes(): NodeInfo[];
  
  // Layout info
  getNodePosition(id: string): { x: number; y: number } | null;
  getNodeDimensions(id: string): { width: number; height: number } | null;
  getEdgePathD(edgeId: string): string | null;
  
  // Programmatic actions
  expandPipeline(id: string): Promise<void>;
  collapsePipeline(id: string): Promise<void>;
  togglePipeline(id: string): Promise<void>;
  setTheme(theme: 'light' | 'dark'): void;
  setSeparateOutputs(value: boolean): void;
  setShowTypes(value: boolean): void;
  fitView(): void;
  zoomTo(level: number): void;
  
  // Debug
  enableDebugOverlays(): void;
  disableDebugOverlays(): void;
  
  // Events (for test synchronization)
  on(event: 'layout-complete' | 'state-change', cb: Function): () => void;
  
  // Validation
  validateConnections(): ValidationResult;
  inspectLayout(): LayoutInspection;
}
```

### Python Test Helper

```python
class PlaywrightVizTester:
    def __init__(self, page: Page):
        self.page = page
    
    async def wait_for_layout(self, timeout: int = 5000):
        await self.page.wait_for_function(
            "window.__hypernodes?.layoutComplete === true",
            timeout=timeout
        )
    
    async def expand_pipeline(self, pipeline_id: str):
        await self.page.evaluate(f"window.__hypernodes.expandPipeline('{pipeline_id}')")
        await self.wait_for_layout()
    
    async def get_node_position(self, node_id: str) -> dict:
        return await self.page.evaluate(f"window.__hypernodes.getNodePosition('{node_id}')")
    
    async def validate_edge_alignment(self) -> dict:
        return await self.page.evaluate("window.__hypernodes.validateConnections()")
    
    async def check_color_contrast(self, node_id: str) -> dict:
        # WCAG contrast checking
        ...
```

---

## Phase 2: Extract & Modularize JavaScript

### Goal
Move JS from Python string to proper TypeScript files with modern build tooling.

### Build Configuration

**package.json:**
```json
{
  "name": "@hypernodes/viz",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "vitest",
    "lint": "eslint src/",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "@xyflow/react": "^12.0.0"
  },
  "devDependencies": {
    "typescript": "^5.0.0",
    "vite": "^5.0.0",
    "vitest": "^1.0.0"
  }
}
```

**vite.config.ts:**
```typescript
export default defineConfig({
  build: {
    lib: {
      entry: 'src/index.ts',
      name: 'HyperNodesViz',
      formats: ['umd'],
      fileName: () => 'hypernodes-viz.umd.js'
    },
    rollupOptions: {
      external: ['react', 'react-dom'],
      output: { globals: { react: 'React', 'react-dom': 'ReactDOM' } }
    }
  }
});
```

### Simplified html_generator.py (After)

```python
def generate_widget_html(graph_data: Dict[str, Any]) -> str:
    react_js = _read_asset("vendor/react.production.min.js", "js")
    react_dom_js = _read_asset("vendor/react-dom.production.min.js", "js")
    elk_js = _read_asset("vendor/elk.bundled.js", "js")
    viz_bundle = _read_asset("dist/hypernodes-viz.umd.js", "js")
    css = _read_asset("styles.css", "css")
    
    return f"""<!DOCTYPE html>
<html>
<head>{css}{react_js}{react_dom_js}{elk_js}</head>
<body>
    <div id="root"></div>
    {viz_bundle}
    <script id="graph-data" type="application/json">{json.dumps(graph_data)}</script>
    <script>HyperNodesViz.mount(document.getElementById('root'))</script>
</body>
</html>"""
```

---

## Phase 3: TypeScript Types

### Shared Type Definitions

```typescript
// api/types.ts
export interface VizNodeData {
  nodeType: 'FUNCTION' | 'PIPELINE' | 'DUAL' | 'BRANCH' | 'DATA' | 'INPUT' | 'INPUT_GROUP';
  label: string;
  typeHint?: string;
  isExpanded?: boolean;
  isBound?: boolean;
  sourceId?: string;
  outputs?: { name: string; type?: string }[];
  params?: string[];
  paramTypes?: string[];
}

export interface VizNode {
  id: string;
  type: 'custom' | 'pipelineGroup';
  position: { x: number; y: number };
  data: VizNodeData;
  parentNode?: string;
  hidden?: boolean;
}

export interface VizEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
}
```

---

## Phase 4: Unit Testing (Vitest)

### Example Tests

```typescript
// src/state/__tests__/applyState.test.ts
describe('applyState', () => {
  it('hides output nodes when separateOutputs=false', () => {
    const nodes = [
      { id: 'fn1', data: { nodeType: 'FUNCTION' } },
      { id: 'out1', data: { nodeType: 'DATA', sourceId: 'fn1' } },
    ];
    const edges = [{ id: 'e1', source: 'fn1', target: 'out1' }];
    
    const result = applyState(nodes, edges, {
      expansionState: new Map(),
      separateOutputs: false,
    });
    
    expect(result.nodes.find(n => n.id === 'out1')).toBeUndefined();
    expect(result.nodes.find(n => n.id === 'fn1')?.data.outputs).toHaveLength(1);
  });
});
```

---

## Phase 5: Storybook + Chromatic (Component Testing)

### Goal
Isolated component testing with automated visual regression detection - the key to safe AI-assisted development.

### Testing Pyramid

```
                    ┌─────────────────┐
                    │   Playwright    │  Full E2E (slow, comprehensive)
                    │   E2E Tests     │  - Full graph rendering
                    └────────┬────────┘  - Real ELK layout
                             │
                    ┌────────┴────────┐
                    │    Chromatic    │  Visual Regression (fast, automated)
                    │  + Storybook    │  - Component screenshots
                    └────────┬────────┘  - Interaction states
                             │
         ┌───────────────────┴───────────────────┐
         │              Vitest                    │  Unit Tests (fastest)
         │         (Logic only)                   │  - State transformations
         └────────────────────────────────────────┘  - Edge compression
```

### Storybook Configuration

**.storybook/main.ts:**
```typescript
import type { StorybookConfig } from '@storybook/react-vite';

const config: StorybookConfig = {
  stories: ['../src/**/*.stories.@(ts|tsx)'],
  addons: [
    '@storybook/addon-essentials',
    '@storybook/addon-interactions',
    '@storybook/addon-a11y',        // Accessibility checks
    '@chromatic-com/storybook',      // Chromatic integration
  ],
  framework: {
    name: '@storybook/react-vite',
    options: {},
  },
};

export default config;
```

**.storybook/preview.ts:**
```typescript
import type { Preview } from '@storybook/react';
import '../src/styles/tailwind.css';

const preview: Preview = {
  parameters: {
    // Chromatic snapshot settings
    chromatic: {
      // Capture at multiple viewports
      viewports: [1280, 1920],
      // Delay for animations to settle
      delay: 300,
    },
    backgrounds: {
      default: 'dark',
      values: [
        { name: 'dark', value: '#0f172a' },   // slate-900
        { name: 'light', value: '#f8fafc' },  // slate-50
      ],
    },
  },
  // Global decorators for ReactFlow context
  decorators: [
    (Story) => (
      <ReactFlowProvider>
        <div style={{ width: 800, height: 600 }}>
          <Story />
        </div>
      </ReactFlowProvider>
    ),
  ],
};

export default preview;
```

### Component Stories

**CustomNode.stories.tsx:**
```typescript
import type { Meta, StoryObj } from '@storybook/react';
import { CustomNode } from './CustomNode';
import { within, userEvent } from '@storybook/testing-library';
import { expect } from '@storybook/jest';

const meta: Meta<typeof CustomNode> = {
  title: 'Nodes/CustomNode',
  component: CustomNode,
  tags: ['autodocs'],
  argTypes: {
    data: { control: 'object' },
  },
};

export default meta;
type Story = StoryObj<typeof CustomNode>;

// ============================================================================
// FUNCTION NODE VARIANTS
// ============================================================================

export const FunctionNode: Story = {
  args: {
    id: 'fn-1',
    data: {
      nodeType: 'FUNCTION',
      label: 'process_data',
      theme: 'dark',
      showTypes: true,
      params: ['input', 'config'],
      paramTypes: ['str', 'dict[str, Any]'],
    },
  },
};

export const FunctionNodeLight: Story = {
  args: {
    ...FunctionNode.args,
    data: { ...FunctionNode.args.data, theme: 'light' },
  },
  parameters: {
    backgrounds: { default: 'light' },
  },
};

export const FunctionNodeWithOutputs: Story = {
  args: {
    id: 'fn-2',
    data: {
      nodeType: 'FUNCTION',
      label: 'extract_features',
      theme: 'dark',
      showTypes: true,
      separateOutputs: false,
      outputs: [
        { name: 'features', type: 'np.ndarray' },
        { name: 'metadata', type: 'dict[str, Any]' },
      ],
    },
  },
};

export const FunctionNodeLongTypeName: Story = {
  name: 'Function Node (Truncated Type)',
  args: {
    id: 'fn-3',
    data: {
      nodeType: 'FUNCTION',
      label: 'complex_transform',
      theme: 'dark',
      showTypes: true,
      params: ['data'],
      paramTypes: ['dict[str, list[tuple[int, float, str]]]'],  // Should truncate
    },
  },
};

// ============================================================================
// PIPELINE NODE VARIANTS
// ============================================================================

export const PipelineCollapsed: Story = {
  args: {
    id: 'pipeline-1',
    data: {
      nodeType: 'PIPELINE',
      label: 'data_processor',
      theme: 'dark',
      isExpanded: false,
    },
  },
};

export const PipelineExpanded: Story = {
  args: {
    id: 'pipeline-2',
    data: {
      nodeType: 'PIPELINE',
      label: 'rag_pipeline',
      theme: 'dark',
      isExpanded: true,
    },
  },
};

// ============================================================================
// DATA NODE VARIANTS
// ============================================================================

export const InputNode: Story = {
  args: {
    id: 'input-1',
    data: {
      nodeType: 'DATA',
      label: 'query',
      typeHint: 'str',
      theme: 'dark',
      showTypes: true,
      isBound: false,
    },
  },
};

export const InputNodeBound: Story = {
  name: 'Input Node (Bound)',
  args: {
    id: 'input-2',
    data: {
      nodeType: 'DATA',
      label: 'model_name',
      typeHint: 'str',
      theme: 'dark',
      showTypes: true,
      isBound: true,
    },
  },
};

export const OutputNode: Story = {
  args: {
    id: 'output-1',
    data: {
      nodeType: 'DATA',
      label: 'result',
      typeHint: 'list[Document]',
      theme: 'dark',
      showTypes: true,
      sourceId: 'fn-1',  // Has a source = output node
    },
  },
};

// ============================================================================
// INPUT GROUP
// ============================================================================

export const InputGroup: Story = {
  args: {
    id: 'group-1',
    data: {
      nodeType: 'INPUT_GROUP',
      label: 'Inputs',
      theme: 'dark',
      showTypes: true,
      params: ['query', 'num_results', 'model'],
      paramTypes: ['str', 'int', 'str'],
      isBound: false,
    },
  },
};

export const InputGroupMixed: Story = {
  name: 'Input Group (Mixed Bound)',
  args: {
    id: 'group-2',
    data: {
      nodeType: 'INPUT_GROUP',
      label: 'Inputs',
      theme: 'dark',
      showTypes: true,
      params: ['query', 'model_name'],
      paramTypes: ['str', 'str'],
      boundParams: [false, true],  // query unbound, model bound
    },
  },
};

// ============================================================================
// INTERACTION TESTS
// ============================================================================

export const PipelineClickToExpand: Story = {
  args: {
    ...PipelineCollapsed.args,
    data: {
      ...PipelineCollapsed.args.data,
      onToggleExpand: () => console.log('Toggle expand'),
    },
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const node = await canvas.findByText('data_processor');
    
    // Verify it starts collapsed
    expect(node).toBeInTheDocument();
    
    // Click to expand
    await userEvent.click(node);
  },
};

// ============================================================================
// ACCESSIBILITY TESTS
// ============================================================================

export const DarkThemeContrast: Story = {
  args: FunctionNode.args,
  parameters: {
    a11y: {
      config: {
        rules: [
          { id: 'color-contrast', enabled: true },
        ],
      },
    },
  },
};

export const LightThemeContrast: Story = {
  args: FunctionNodeLight.args,
  parameters: {
    backgrounds: { default: 'light' },
    a11y: {
      config: {
        rules: [
          { id: 'color-contrast', enabled: true },
        ],
      },
    },
  },
};
```

### Full Graph Stories

**GraphView.stories.tsx:**
```typescript
import type { Meta, StoryObj } from '@storybook/react';
import { GraphView } from './GraphView';

// Test fixtures - predefined graph data
import { simpleGraph, nestedGraph, ragGraph, complexGraph } from '../fixtures';

const meta: Meta<typeof GraphView> = {
  title: 'Graphs/GraphView',
  component: GraphView,
  parameters: {
    layout: 'fullscreen',
    chromatic: {
      viewports: [1280, 1920],
      delay: 500,  // Wait for ELK layout
    },
  },
};

export default meta;
type Story = StoryObj<typeof GraphView>;

// ============================================================================
// SIMPLE GRAPHS
// ============================================================================

export const SimpleLinear: Story = {
  name: 'Simple Linear Pipeline',
  args: {
    graphData: simpleGraph.linear,
    theme: 'dark',
    separateOutputs: true,
  },
};

export const SimpleBranching: Story = {
  name: 'Simple Branching',
  args: {
    graphData: simpleGraph.branching,
    theme: 'dark',
    separateOutputs: true,
  },
};

// ============================================================================
// NESTED PIPELINES
// ============================================================================

export const NestedCollapsed: Story = {
  name: 'Nested Pipeline (Collapsed)',
  args: {
    graphData: nestedGraph.data,
    expansionState: { inner_pipeline: false },
    theme: 'dark',
  },
};

export const NestedExpanded: Story = {
  name: 'Nested Pipeline (Expanded)',
  args: {
    graphData: nestedGraph.data,
    expansionState: { inner_pipeline: true },
    theme: 'dark',
  },
};

// ============================================================================
// RAG PIPELINE (COMPLEX REAL-WORLD)
// ============================================================================

export const RAGFullyCollapsed: Story = {
  name: 'RAG Pipeline (All Collapsed)',
  args: {
    graphData: ragGraph.data,
    expansionState: {
      retrieval: false,
      generation: false,
      evaluation: false,
    },
    theme: 'dark',
  },
};

export const RAGFullyExpanded: Story = {
  name: 'RAG Pipeline (All Expanded)',
  args: {
    graphData: ragGraph.data,
    expansionState: {
      retrieval: true,
      generation: true,
      evaluation: true,
    },
    theme: 'dark',
  },
};

export const RAGPartialExpand: Story = {
  name: 'RAG Pipeline (Retrieval Expanded)',
  args: {
    graphData: ragGraph.data,
    expansionState: {
      retrieval: true,
      generation: false,
      evaluation: false,
    },
    theme: 'dark',
  },
};

// ============================================================================
// THEME VARIANTS
// ============================================================================

export const DarkTheme: Story = {
  args: {
    graphData: nestedGraph.data,
    theme: 'dark',
  },
  parameters: {
    backgrounds: { default: 'dark' },
  },
};

export const LightTheme: Story = {
  args: {
    graphData: nestedGraph.data,
    theme: 'light',
  },
  parameters: {
    backgrounds: { default: 'light' },
  },
};

// ============================================================================
// OUTPUT MODES
// ============================================================================

export const SeparateOutputs: Story = {
  args: {
    graphData: simpleGraph.multiOutput,
    separateOutputs: true,
    theme: 'dark',
  },
};

export const CombinedOutputs: Story = {
  args: {
    graphData: simpleGraph.multiOutput,
    separateOutputs: false,
    theme: 'dark',
  },
};

// ============================================================================
// EDGE CASES
// ============================================================================

export const ManyInputs: Story = {
  name: 'Many Inputs (Grouped)',
  args: {
    graphData: complexGraph.manyInputs,
    theme: 'dark',
  },
};

export const DeepNesting: Story = {
  name: 'Deep Nesting (3 levels)',
  args: {
    graphData: complexGraph.deepNesting,
    expansionState: {
      level1: true,
      level2: true,
      level3: true,
    },
    theme: 'dark',
  },
};

export const LongLabels: Story = {
  name: 'Long Labels (Truncation)',
  args: {
    graphData: complexGraph.longLabels,
    theme: 'dark',
    showTypes: true,
  },
};
```

### Chromatic CI Integration

**GitHub Actions Workflow (.github/workflows/chromatic.yml):**
```yaml
name: Chromatic

on:
  push:
    branches: [main]
    paths:
      - 'src/hypernodes/viz/js/**'
  pull_request:
    paths:
      - 'src/hypernodes/viz/js/**'

jobs:
  chromatic:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Required for Chromatic to detect changes
      
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
          cache-dependency-path: src/hypernodes/viz/js/package-lock.json
      
      - name: Install dependencies
        working-directory: src/hypernodes/viz/js
        run: npm ci
      
      - name: Run Chromatic
        uses: chromaui/action@latest
        with:
          workingDir: src/hypernodes/viz/js
          projectToken: ${{ secrets.CHROMATIC_PROJECT_TOKEN }}
          # Fail the build if there are visual changes (for PRs)
          exitOnceUploaded: ${{ github.event_name == 'push' }}
          exitZeroOnChanges: ${{ github.event_name == 'push' }}
          # Auto-accept changes on main branch
          autoAcceptChanges: main
```

### Package.json Updates

```json
{
  "name": "@hypernodes/viz",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "vitest",
    "test:ui": "vitest --ui",
    "lint": "eslint src/",
    "typecheck": "tsc --noEmit",
    "storybook": "storybook dev -p 6006",
    "build-storybook": "storybook build",
    "chromatic": "chromatic --exit-zero-on-changes"
  },
  "devDependencies": {
    "@chromatic-com/storybook": "^1.0.0",
    "@storybook/addon-a11y": "^8.0.0",
    "@storybook/addon-essentials": "^8.0.0",
    "@storybook/addon-interactions": "^8.0.0",
    "@storybook/react": "^8.0.0",
    "@storybook/react-vite": "^8.0.0",
    "@storybook/test": "^8.0.0",
    "chromatic": "^11.0.0",
    "storybook": "^8.0.0"
  }
}
```

### What Chromatic Catches Automatically

| Scenario | Detection |
|----------|-----------|
| Node color/style changes | Pixel diff on component |
| Layout shifts | Position change detection |
| Missing text/labels | Content diff |
| Truncation changes | Visual comparison |
| Theme regressions | Dark/light story comparison |
| Hover state changes | Interaction story diffs |
| Edge path changes | SVG path comparison |
| Accessibility violations | a11y addon integration |

### Benefits for AI-Assisted Development

1. **Every PR gets visual review** - Chromatic blocks merge if regressions detected
2. **Clear visual diffs** - See exactly what pixels changed
3. **No manual testing needed** - Screenshots captured automatically
4. **Component isolation** - Test each variant independently
5. **Interaction testing** - Verify expand/collapse behavior
6. **Accessibility built-in** - WCAG violations flagged automatically
7. **Fast feedback loop** - Results in ~2-3 minutes

---

## Phase 6: Playwright E2E Tests

### Goal
Full integration tests that exercise the complete viz from Python → HTML → Browser.

### When to Use Playwright vs Storybook/Chromatic

| Use Case | Tool |
|----------|------|
| Component styling/layout | Storybook + Chromatic |
| Theme switching | Storybook + Chromatic |
| Node expand/collapse visual | Storybook + Chromatic |
| Full Python → JS data flow | Playwright |
| Real ELK layout behavior | Playwright |
| Edge connection validation | Playwright |
| Performance/load testing | Playwright |
| Accessibility (full page) | Playwright + axe-core |

### Playwright Test Examples

```python
# tests/viz/e2e/test_full_integration.py
import pytest
from playwright.sync_api import Page, expect

class TestFullIntegration:
    """Tests requiring full Python → JS pipeline."""
    
    @pytest.fixture
    def viz_page(self, page: Page, pipeline):
        """Render actual pipeline to HTML and load in browser."""
        from hypernodes.viz import UIHandler
        from hypernodes.viz.js.html_generator import generate_widget_html
        
        handler = UIHandler(pipeline, depth=99)
        html = generate_widget_html(handler.get_visualization_data())
        
        # Load in browser
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as f:
            f.write(html.encode())
        page.goto(f'file://{f.name}')
        page.wait_for_function("window.__hypernodes?.layoutComplete")
        return page
    
    def test_edge_alignment_after_collapse(self, viz_page: Page):
        """Verify edges connect properly after collapse."""
        # Collapse a pipeline
        viz_page.evaluate("window.__hypernodes.collapsePipeline('inner')")
        viz_page.wait_for_function("window.__hypernodes?.layoutComplete")
        
        # Validate connections
        result = viz_page.evaluate("window.__hypernodes.validateConnections()")
        assert result['valid'], f"Edge issues: {result['issues']}"
    
    def test_programmatic_expand_all(self, viz_page: Page):
        """Expand all pipelines programmatically."""
        pipelines = viz_page.evaluate("""
            window.__hypernodes.getAllNodes()
                .filter(n => n.data.nodeType === 'PIPELINE')
                .map(n => n.id)
        """)
        
        for pid in pipelines:
            viz_page.evaluate(f"window.__hypernodes.expandPipeline('{pid}')")
            viz_page.wait_for_function("window.__hypernodes?.layoutComplete")
        
        # All should be expanded now
        state = viz_page.evaluate("window.__hypernodes.getExpansionState()")
        assert all(state.values()), "Not all pipelines expanded"


class TestAccessibility:
    """WCAG compliance tests."""
    
    def test_wcag_aa_compliance(self, viz_page: Page):
        """Run axe-core accessibility audit."""
        # Inject axe-core
        viz_page.add_script_tag(url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.2/axe.min.js")
        
        result = viz_page.evaluate("""
            async () => {
                const results = await axe.run();
                return {
                    violations: results.violations,
                    passes: results.passes.length,
                };
            }
        """)
        
        assert len(result['violations']) == 0, f"a11y violations: {result['violations']}"


class TestPerformance:
    """Performance benchmarks."""
    
    def test_large_graph_render_time(self, viz_page: Page, large_pipeline):
        """Verify large graphs render in reasonable time."""
        start = viz_page.evaluate("performance.now()")
        viz_page.wait_for_function("window.__hypernodes?.layoutComplete")
        end = viz_page.evaluate("performance.now()")
        
        render_time = end - start
        assert render_time < 5000, f"Render took {render_time}ms (>5s threshold)"
```

---

## Phase 7: Migration Timeline

| Week | Tasks |
|------|-------|
| 1 | Setup `js/` directory, package.json, tsconfig, vite config |
| 2 | Extract `state_utils.js` → TypeScript, add unit tests (Vitest) |
| 3 | Extract components to .tsx, build UMD bundle |
| 4 | Setup Storybook, write component stories for all node types |
| 5 | Setup Chromatic, integrate with GitHub Actions |
| 6 | Implement VizController API, expose on window.__hypernodes |
| 7 | Setup Playwright E2E tests for integration scenarios |
| 8 | Update html_generator.py, cleanup, documentation |

---

## Open Questions

### 1. Build Tool
- **Vite** - Fast, modern, good DX, larger ecosystem (recommended)
- **esbuild** - Simpler, lighter, faster builds

### 2. TypeScript Strictness
- **Strict mode** - Best type safety, more work upfront (recommended for new code)
- **Loose mode** - Easier migration, less safety
- **JSDoc only** - No build step, still get IDE support

### 3. State Logic Duplication
- **Option A:** Keep `state_simulator.py` separate (manual sync)
- **Option B:** Generate Python from TypeScript
- **Option C:** Run JS via Node subprocess (single source of truth) (recommended)

### 4. Vendor Bundling
- **Separate files** - Current approach, easier updates (recommended)
- **Single bundle** - Simpler deployment, larger file
- **Import maps** - Modern browsers only

### 5. Chromatic Plan
- **Free tier** - 5,000 snapshots/month (likely enough for this project)
- **Pro tier** - Unlimited snapshots, TurboSnap, more features
- **Self-hosted alternative** - Percy, Argos, or Playwright screenshots (no cloud)

### 6. Story Coverage Priority
What scenarios are most important to cover in Storybook?
- All node type variants (FUNCTION, PIPELINE, DATA, etc.)?
- All theme combinations (dark/light)?
- All expansion states?
- Edge cases (truncation, many inputs, deep nesting)?
- All of the above? (recommended)

---

## Complete Testing Strategy Summary

### Testing Layers

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              CI/CD Pipeline                                 │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  1. Lint & Typecheck (ESLint + TypeScript)          ~10 seconds           │
│     └─ Catches: Syntax errors, type mismatches, code style               │
│                                                                            │
│  2. Unit Tests (Vitest)                             ~30 seconds           │
│     └─ Catches: Logic bugs in state transformations                      │
│                                                                            │
│  3. Component Tests (Storybook + Chromatic)         ~2 minutes            │
│     └─ Catches: Visual regressions, styling bugs, accessibility          │
│     └─ Runs on: Every PR touching viz/js                                 │
│     └─ Blocks merge: If visual diff detected                             │
│                                                                            │
│  4. E2E Tests (Playwright)                          ~5 minutes            │
│     └─ Catches: Integration bugs, Python→JS data flow, edge alignment    │
│     └─ Runs on: Every PR, nightly for full suite                         │
│                                                                            │
│  5. Python Tests (pytest)                           ~1 minute             │
│     └─ Catches: state_simulator parity, UIHandler bugs                   │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### Test File Organization

```
src/hypernodes/viz/js/
├── src/
│   ├── components/
│   │   ├── CustomNode.tsx
│   │   ├── CustomNode.stories.tsx      # Storybook stories
│   │   └── CustomNode.test.tsx         # Unit tests (if needed)
│   ├── state/
│   │   ├── applyState.ts
│   │   └── __tests__/
│   │       └── applyState.test.ts      # Vitest unit tests
│   └── ...
├── .storybook/                         # Storybook config
└── vitest.config.ts

tests/
├── viz/
│   ├── e2e/                            # Playwright E2E
│   │   ├── test_full_integration.py
│   │   ├── test_accessibility.py
│   │   └── conftest.py
│   ├── test_state_simulator.py         # Python state tests
│   └── test_edge_alignment.py          # Existing tests
```

### When Each Test Type Runs

| Trigger | Lint | Unit | Storybook | Playwright | Python |
|---------|------|------|-----------|------------|--------|
| Pre-commit hook | Yes | Yes | No | No | No |
| PR (viz changes) | Yes | Yes | Yes (Chromatic) | Yes | Yes |
| PR (other changes) | No | No | No | No | Yes |
| Merge to main | Yes | Yes | Yes (auto-accept) | Yes | Yes |
| Nightly | Yes | Yes | No | Full suite | Yes |

### Chromatic Workflow

1. **Developer pushes PR** → Chromatic captures screenshots
2. **Visual diff detected** → PR blocked, review required
3. **Developer reviews diff** → Accept or fix
4. **All accepted** → PR can merge
5. **Merge to main** → New baseline set

This workflow ensures:
- No visual regressions slip through
- AI-generated code changes are validated visually
- Manual testing is minimized
- Fast feedback (<3 min for visual check)

---

## Next Steps

1. Answer the 6 open questions above
2. Approve or modify the plan  
3. Begin Phase 1 implementation (build tooling setup)
