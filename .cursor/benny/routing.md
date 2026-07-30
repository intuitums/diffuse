# Diffuse routing map

Triage may classify without a destination. Do not guess owners or channels.

```yaml
routes:
  - name: "review-engine"
    match:
      product_areas:
        - "pull-request review"
        - "findings"
        - "github checks"
      code_paths:
        - "service/"
        - "indexer/"
        - "retriever/"
      error_signatures: []
    destination:
      slack_channel: ""
      tracker_team: ""
    owners: []
    allow_feature_owner_ping: false

  - name: "onboarding-and-indexing"
    match:
      product_areas:
        - "repository onboarding"
        - "indexing"
        - "mirror"
      code_paths:
        - "service/"
        - "sql/"
      error_signatures: []
    destination:
      slack_channel: ""
      tracker_team: ""
    owners: []
    allow_feature_owner_ping: false

  - name: "mcp-api-cli"
    match:
      product_areas:
        - "mcp"
        - "rest api"
        - "cli"
      code_paths:
        - "service/"
      error_signatures: []
    destination:
      slack_channel: ""
      tracker_team: ""
    owners: []
    allow_feature_owner_ping: false

fallback:
  destination: ""
  owners: []
  allow_feature_owner_ping: false

ping_policy:
  default: "off"
  allow:
    - "configured-feature-owner"
    - "confirmed-regression-author"
  deny:
    - "broad-on-call-group"
    - "unverified-owner"
```
