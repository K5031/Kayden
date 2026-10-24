# Frontend API and contributions

**Status:** Implemented, with a deliberately narrow contribution model.

## Purpose

Frontends should be replaceable without knowing application-runtime internals.
At the same time, a module should be able to expose an ordinary user control,
such as microphone mute, without adding a feature-specific method to every
frontend interface.

These are separate contracts:

- `FrontendInterface` owns frontend lifecycle through `run(api)`.
- `FrontendAPI` carries typed commands and events.
- `ControlContribution` lets a module declare an optional generic control.

## Frontend API

Commands travel from a frontend to the runtime. They cover message submission,
interruption, model and module selection, input capture, contributed-control
invocation, and permission resolution.

Events travel from the runtime to the frontend. They carry identity and
configuration, restored and live conversation content, status, tool activity,
capture state, contributed controls, and permission requests.

The API is transport-neutral in shape but currently in-process. A frontend may
subscribe one event listener. Events published before subscription are queued
and replayed once, allowing configuration and transcript restoration before a
platform event loop starts. Only the runtime owns the publishing side.

```text
Qt / TUI / Tauri
    | typed commands
    v
 FrontendAPI
    ^
    | typed events
application runtime
```

This boundary is intended to make a future separate-process or remote frontend
possible without exposing the application object graph, but no network protocol
or compatibility-version scheme exists yet.

## Control contributions

A contributing module returns `ControlContribution` values from an optional
`contributions()` method. Each contribution declares:

- a namespaced ID and human-readable label;
- a generic kind: button, toggle, or selection;
- a placement hint: header, composer, or menu;
- current value, enabled state, icon hint, and selection options;
- an invocation callback owned by the provider;
- optional operation metadata for permission policy.

The application runtime discovers contributions across swappable capabilities,
rejects duplicate IDs or unsupported values, publishes presentation-only
descriptors, and retains the callbacks privately. A frontend renders only the
generic controls it understands. On interaction it sends `InvokeControl`; the
runtime authorizes and routes the invocation, then republishes all control state.

The provider owns the authoritative state. A frontend must not maintain a
separate mute/model/etc. truth beyond temporary display state.

Control contributions are not frontend modules and are not accepted directly by
arbitrary modules. Today the application runtime is their registry and frontends
are their consumers. Additional contribution types may be introduced only for a
concrete cross-module need, with their own typed contract; this is not intended
to become an untyped universal UI description language.

## Module switching

Module discovery and switching are runtime functionality exposed through generic
frontend commands and events, not Qt-only behavior. The runtime publishes a
catalog of swappable categories and installed choices. A frontend chooses how to
present it and submits a complete selection update; the runtime validates,
prepares, swaps, persists, and republishes configuration.

## Invariants

- Frontends communicate through typed commands and events, not runtime internals.
- Frontends never receive provider callbacks or module instances.
- Contribution IDs are namespaced and unique among active providers.
- A contribution descriptor contains data only; execution remains runtime-side.
- Provider modules own control state and behavior.
- Permission checks occur before a contributed callback runs.
- Removing or swapping a provider removes its controls on the next refresh.
- Unsupported optional controls may be omitted by a less capable frontend; core
  conversation behavior must remain usable.

## Trade-offs and limits

A small generic vocabulary keeps frontends independent, but cannot express an
arbitrary custom interface such as a rich memory editor or 3D visualization.
Those should not be forced into `ControlContribution`; a concrete need should
first define a constrained contribution contract.

The API currently supports one attached event subscriber, and lifecycle still
assumes an in-process frontend. Multi-client state synchronization, protocol
versioning, and a general contribution router remain future work. See
[Runtime and modules](runtime-and-modules.md#proposed-equal-routing-for-static-and-dynamic-modules).
