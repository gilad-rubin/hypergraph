---
name: test-matrix-analysis
description: Creates comprehensive N-dimensional test matrices for codebases. Use when asked to analyze test coverage, identify testing gaps, create test plans, or review what tests exist vs what's needed.
---

# Test Matrix Analysis

A systematic methodology for analyzing test coverage by modeling the test space as an N-dimensional matrix, mapping existing tests, and identifying gaps.

## When to Use

- User asks to review test coverage
- User wants to know what tests are missing
- User asks to create a test plan or test strategy
- User wants to understand test gaps before a release
- User asks "what should we test?" or "are we testing enough?"

## Methodology

### Phase 1: Understand the Codebase

Before creating the matrix, gather deep context. **Run both explorations in parallel** using Task agents to save time:

1. **Explore architecture**: Use the explore agent to understand:
   - Main purpose/domain of the project
   - Key components, modules, and classes
   - Main data structures and relationships
   - External integrations/APIs
   - Configuration options and feature flags

2. **Explore existing tests**: Use the explore agent to catalog:
   - All test files and their locations
   - What each test file covers
   - Testing patterns used (unit, integration, fixtures, mocks)
   - Current coverage quality per module
   - Test naming conventions and organization

### Phase 2: Define the N-Dimensional Matrix

Identify **orthogonal dimensions** that define the test space. Common dimensions include:

| Dimension Type | Examples |
|----------------|----------|
| **Component Types** | Node types, service types, model types |
| **Execution Modes** | sync, async, generator, streaming |
| **Data Structures** | Topologies, schemas, relationships |
| **Input Variations** | Sources, types, edge cases (None, empty, large) |
| **Type System** | Simple types, generics, unions, protocols |
| **Configuration** | Feature flags, modes, limits |
| **Error Conditions** | Validation errors, runtime errors, edge cases |
| **External Integrations** | APIs, databases, file systems |

For each dimension, enumerate all possible values with descriptions.

### Phase 3: Calculate Complexity

Show the theoretical test space size:

```
Total Combinations = dim1_values × dim2_values × ... × dimN_values
```

This demonstrates why exhaustive testing is intractable and justifies prioritization.

### Phase 4: Create Prioritized Test Slices

Instead of testing all combinations, create **2D slices** that cover high-value intersections:

1. **Slice by Risk × Usage**: Combinations likely to have bugs AND commonly used
2. **Slice by Independence**: Orthogonal dimensions can be tested separately
3. **Slice by Complexity**: Simple combinations first, then complex

For each slice, create a table showing coverage status:
- Y = Full coverage
- P = Partial coverage  
- N = No coverage
- N/A = Not applicable

Example slice format:
```
| Topology | SyncRunner | AsyncRunner |
|----------|:----------:|:-----------:|
| linear   | Y          | Y           |
| diamond  | Y          | P           |
| cycle    | Y          | N           |

**Status**: GOOD - `test_runners/` covers most combinations
**Gap**: Cycle topology with AsyncRunner needs coverage
```

### Phase 5: Map Existing Tests

Create a matrix mapping test files to dimensions:

| Test File | Dim1 | Dim2 | Dim3 | ... |
|-----------|:----:|:----:|:----:|:---:|
| test_foo.py | Y | P | N/A | ... |

This reveals which dimensions have good coverage vs gaps.

### Phase 6: Gap Analysis

For each gap, document with concrete test recommendations:

```markdown
#### GAP-XX: [Descriptive Name]
**Matrix Position**: Dimension1 = value × Dimension2 = value
**Risk**: [Why this gap matters]
**Recommended Tests**:
```python
class TestGapName:
    def test_specific_scenario(self): ...
    def test_edge_case(self): ...
```
```

**Focus on intersections, not individual dimensions** - gaps are most dangerous where multiple dimensions combine in untested ways.

Prioritize gaps as HIGH / MEDIUM / LOW based on:
- **Risk**: Likelihood of bugs in this area
- **Impact**: Severity if bugs exist
- **Usage**: How often this code path is used

### Phase 7: Recommendations

Provide actionable output:

1. **Coverage Score**: X/100 with per-dimension breakdown
2. **Top N Action Items**: Prioritized list of gaps to address
3. **New Test Files**: Suggested file names and estimated test counts
4. **Tests to Add to Existing Files**: Specific additions per file
5. **Test Type Distribution**: Current vs recommended (unit, integration, property-based, performance)

## Output Format

Generate a markdown document with these sections:

```markdown
# [Project Name] Test Matrix Review

## Overview
[Brief description of methodology and findings]

## 1. Conceptual Test Matrix (N-Dimensional)
[Tables defining each dimension and its values]

## 2. Full Matrix Combinations
[Calculation showing test space size]

## 3. Prioritized Test Slices
[2D matrices with coverage status]

## 4. Existing Test Coverage Map
[Test file × dimension matrix]

## 5. Gap Analysis & Recommended Tests
[Prioritized gaps with specific test recommendations]

## 6. Test Type Distribution
[Current vs recommended distribution]

## 7. Summary
[Coverage score, action items, files to create]

## Appendix: Test Matrix Visualization
[ASCII diagram showing test space structure]
```

## Best Practices

1. **Run Phase 1 explorations in parallel** - Use multiple Task agents to gather architecture and test info simultaneously
2. **Be exhaustive in dimension discovery** - Missing a dimension means missing test gaps
3. **Use domain terminology** - Dimensions should match how developers think about the code
4. **Focus on intersections** - Gaps at dimension intersections are more dangerous than single-dimension gaps
5. **Quantify everything** - Scores, counts, and percentages make gaps concrete and trackable over time
6. **Provide code snippets** - Show actual test class/method signatures for recommendations
7. **Consider test types** - Not just what to test, but how (unit, integration, property-based)
8. **Include edge cases** - Empty collections, None values, boundary conditions
9. **Track error conditions** - Both build-time validation and runtime errors

## Example Dimensions by Domain

### Web API
- HTTP methods, endpoints, auth states, request body types, response codes, error types

### Data Pipeline  
- Source types, transformations, sinks, data formats, error handling, parallelism modes

### State Machine
- States, transitions, events, guards, actions, error recovery

### Compiler/Parser
- Token types, AST nodes, semantic rules, optimization passes, target outputs
