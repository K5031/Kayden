# Kayden Tauri Frontend Module

This directory is the native client owned by Kayden's `frontend/tauri` module.
It is a Tauri 2 shell around a React/TypeScript interface and contains
presentation code only; Kayden's Python process owns conversations,
credentials, memory, and tool execution.

## Development

Install the current Node.js LTS release, Rust, and Tauri's platform
prerequisites. Then install the frontend dependencies:

```bash
cd src/kayden/modules/frontend/tauri/desktop
npm install
```

Select `frontend = "tauri"` in Kayden's config and run `kayden` from the
repository environment. The Python frontend module starts an authenticated
loopback bridge and launches `npm run tauri dev` automatically. Configure
`mode = "floaty"` under `[settings.frontend.tauri]` to use its compact
always-on-top layout; the default mode is the full desktop window.

To launch a previously built desktop binary instead, set:

```bash
export KAYDEN_DESKTOP_BINARY=/path/to/kayden-desktop
```

## Build

```bash
npm run build
npm run tauri build
```

The web build validates TypeScript and produces static assets. The Tauri build
also requires the platform-native libraries described in Tauri's prerequisite
guide and produces the appropriate desktop bundles for the current operating
system.
