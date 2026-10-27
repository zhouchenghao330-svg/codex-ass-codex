# Vendored Codex Mermaid renderer

Source: https://github.com/openai/codex/tree/a9b0815167c27ab13719dd12d13b63e02b70d221/codex-rs/mermaid
Revision: a9b0815167c27ab13719dd12d13b63e02b70d221
License: Apache-2.0; see LICENSE and NOTICE in this directory.

Cargo.toml replaces workspace inheritance with standalone package and dependency settings. Room owns Markdown fence detection, fallback notices and display integration outside this crate.

Local patches:
- Flowchart node and edge labels support `<br>`, `<br/>` and `<br />` (case insensitive) as bounded multiline text; other HTML and entities remain unsupported.
- Graph drawing lays out multiline labels inside their original node shapes and reserves edge-label rows/gutters without changing arrows or connectivity.
- `render_vertical_spans` explicitly reflows LR/RL flowcharts into TD/BT without changing nodes or edges. Room only calls it when the source layout is too wide, and shows a layout notice.
- Regression tests cover multiline labels, bounds, layout semantics and unsupported markup. Upstream snapshots remain unchanged.
