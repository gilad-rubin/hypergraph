# Nodes Agent Guide

Node metadata feeds graph validation, runners, visualization, and docs. Treat
shape changes as public-ish behavior.

## Route Metadata

- Branch labels and descriptions are user-authored text; they are not
  guaranteed unique. Do not key storage by description unless construction-time
  validation rejects duplicates.
- Gates should fail early on ambiguous configuration. Silent target loss is not
  acceptable.
- Changing `branch_data` shape or route description semantics needs maintainer
  input and coordinated tests for nodes, viz, and docs consumers.
