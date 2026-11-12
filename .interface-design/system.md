# Design System — Pipeline Scraping Operation

## Direction & Feel
- **Persona:** Industrial E-commerce & PIM Operations Console ("Bancada de Controle Industrial").
- **Feel:** High-clarity operational workbench — warm light surfaces to reduce visual fatigue during long catalog review sessions, paired with a high-contrast dark terminal log console and semantic industrial accents.
- **Target Viewport:** Responsive from `1366x768` up to `1240px+` container width.

## Color Tokens
- **Surfaces:**
  - `--fundo`: `#f3f0e8` (warm industrial parchment canvas)
  - `--papel`: `#fffdf8` (elevated card surface)
  - `--papel-sutil`: `#f9f6ee` (inset control groups & subtle backgrounds)
  - Terminal Log Surface: `#141920` with `#262f3d` border
- **Typography / Ink:**
  - `--tinta`: `#1d1a16` (primary text)
  - `--tinta-2`: `#3a352e` (secondary labels)
  - `--muted`: `#5e584f` (captions, metadata, uppercase section headers)
- **Borders:**
  - `--linha`: `#e2dbd0` (standard card border)
  - `--linha-forte`: `#cfc6b8` (inputs, secondary buttons, hover borders)
- **Semantic Accents:**
  - `--azul`: `#1d4e89` / `--azul-fraco`: `#e7f0fa` (discovery, collection, system info)
  - `--verde`: `#0f6e56` / `--verde-escuro`: `#0b5240` / `--verde-fraco`: `#e5f4ef` (active execution, completed, exported, primary CTA)
  - `--ambar`: `#8a5a00` / `--ambar-fraco`: `#fff4d8` (warnings, bottlenecks, queued/running pulse)
  - `--vermelho`: `#9f2d2d` / `--vermelho-fraco`: `#fdecec` (stopped state, failures, destructive actions)
  - `--neutro`: `#64748b` / `--neutro-fraco`: `#f1f5f9` (filtered zero-cost SKUs)

## Depth, Spacing & Typography
- **Depth Strategy:** Layered subtle shadow (`--sombra-card: 0 1px 2px rgba(29,26,22,0.04), 0 8px 24px rgba(29,26,22,0.06)`) + `1px solid var(--linha)` border.
- **Border Radius Scale:** `8px` controls/buttons · `10px` inner panels/banners · `14px` primary cards/containers · `999px` status pills.
- **Typography Scale:**
  - Font stack: `"Segoe UI", -apple-system, BlinkMacSystemFont, system-ui, sans-serif`
  - Monospace stack: `"Cascadia Code", Consolas, "Courier New", monospace`
  - Section/KPI labels: `11px` · `700` weight · `uppercase` · `0.04em–0.07em` tracking
  - Body/Controls: `13px`
  - Card titles (`h2`): `15px` · `700` weight
  - KPI Hero Numbers: `22px` · `700` weight · `tabular-nums`

## Key Component Patterns
- **Top Bar (`.topo-barra`):** Brand icon badge + title + semantic live status pill (`#badge-topo-status`: `.parado` red / `.rodando` green) + tab navigation + secondary `Atualizar` and `.outline-perigo` `Restaurar contadores`.
- **KPI Metric Cards (`.kpi-card`):** 7-column grid on desktop (`1200px+`), `3px` semantic top border, subtle vertical tint, `22px` tabular number, inline SVG sparkline (`.kpi-spark`), and `54px` watermark background SVG icon (`.kpi-ico-bg` at `0.08` opacity).
- **Visual Pipeline Funnel (`.funil-visual`):** 5 progressive chevron stages (`clip-path: polygon(...)`) stepping from `#1d4e89` to `#128264`, with hover tooltips (`.funil-tooltip`) showing stage-to-stage conversion percentages and a bottom retention breakdown.
- **Phase-Grouped Action Cards (`.fase-grupo` / `.etapa`):** Grouped into **Fase 1 · Coleta**, **Fase 2 · Processamento**, and **Fase 3 · Publicação**. Each card displays a semantic status dot (`.dot-status`), cumulative badge, metadata, and an outline green play button (`.btn-etapa`) that fills solid green (`.ativo`) only while running.
- **Execution Form (`#form`):** Custom-styled selects/inputs with blue focus ring (`rgba(29,78,137,0.14)`), grouped checkboxes (`.checks-modernos`), and a prominent gradient primary CTA (`.btn-cta-pipeline`).
- **High-Contrast Log Console (`.log`):** Dark terminal (`#141920`), level-colored left borders and badges (`INFO` `#38bdf8`, `WARN` `#f59e0b`, `ERROR` `#ef4444`), keyword search (`#filtro-busca-log`), `Limpar Logs` (`#limpar-logs`), and `Rolar automaticamente` (`#log-autoscroll`).
