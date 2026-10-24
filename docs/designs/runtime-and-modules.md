# Runtime and modules

**Status:** Current architecture, with one proposed evolution called out below.

## Purpose

Kayden is a fixed application runtime assembled from replaceable modules. A
module owns one capability; the runtime owns how those capabilities are combined
into the Kayden application. Replacing the orchestration itself would produce a
different application, so there is deliberately no controller module.

The current capability categories are frontend, input, output, core, context,
memory, action, and tools. Input and output are optional. Output and tools may
have multiple active implementations; the other categories have one.

## Module contract and layout

Each category has a small abstract interface in `kayden.interfaces`. An installed
module is discovered by its package name and must export a `Module` class that
inherits the interface for that category. The registry rejects a class with the
wrong interface before instantiating it.

A module that fits comfortably in one file may be a single module file. A module
that needs several files is a package: `main.py` contains the interface
implementation and primary behavior, while `__init__.py` exports `Module`.
Supporting files may use whatever names suit the implementation. Helpers such as
Qt objects may inherit framework classes; only the exported `Module` class is
required to implement Kayden's category interface.

Modules receive configuration through constructor keyword arguments. They must
not read another module's private state or import a concrete peer implementation.
Shared contracts live outside module packages.

## Ownership

The registry owns installed module instances and their configured selections. It
handles discovery, construction, interface validation, lookup, replacement, and
reverse-order shutdown. A multi-module adapter fans calls out to active output or
tool modules.

The application runtime owns orchestration:

```text
frontend command
      |
      v
application runtime
  | core + action + tools
  | context + memory
  | optional input/output
      |
      v
frontend events
```

Modules do not choose the turn sequence. The runtime builds a turn, asks the
action module to drive the core and tools, records memory, applies context
policy, publishes frontend events, and coordinates interruption and shutdown.

Some cross-capability adaptation is still explicit in the runtime. In
particular, memory-provided actions are borrowed by the tool runtime, core and
context must agree on token-usage accounting, and manual input is surfaced as
frontend capture commands. These are current implementation facts, not the
desired general communication model.

## Hot swapping

A configured swap follows this sequence:

1. Discover and construct every requested replacement without publishing it.
2. Validate category interfaces and core/context compatibility.
3. Bind runtime services such as tool authorization.
4. Wait on the turn gate so publication occurs between turns.
5. Replace the registry snapshot atomically.
6. Refresh borrowed tools, workers, frontend configuration, and contributions.
7. Stop displaced modules after publication.

If preparation fails, the active set is untouched and prepared candidates are
stopped. If registry publication fails, the old snapshot remains active. The
frontend itself cannot be replaced while attached; it must first disconnect.

This is safe-boundary replacement, but it is not yet a general start/readiness,
drain, or disposable-resource protocol. Module constructors currently perform
any preparation they require, and optional `stop()` methods perform cleanup.

## Invariants

- The runtime, not a replaceable controller, defines Kayden's behavior.
- Every registered module implements the interface for its category.
- Required capabilities cannot be removed.
- A replacement is fully constructed and validated before it becomes visible.
- Publication happens between turns under the turn gate.
- Failure to prepare a replacement must not disturb active modules.
- Displaced modules are stopped only after a successful replacement.
- A core that reports provider usage requires a `reported` context policy; other
  cores require a `self`-accounting context policy.

## Trade-offs

Category interfaces make implementations replaceable and understandable, but
they do not by themselves provide process isolation or prevent modules from
calling arbitrary Python APIs. The fixed runtime keeps Kayden coherent, at the
cost of making new kinds of orchestration a runtime change rather than a plugin.

Hot swapping minimizes downtime but is not zero-risk for modules with unmanaged
background work. A future resource-registration mechanism should let the runtime
dispose subscriptions and workers reliably.

## Proposed: equal routing for static and dynamic modules

This section is a reviewed direction, not current behavior.

Kayden should eventually give all module categories the same runtime-owned
communication mechanism. Modules would declare one of two reception modes:

- **Static:** invoked through request -> execution -> result and cannot receive
  unsolicited routed input.
- **Dynamic:** may also subscribe to unsolicited contributions or events routed
  by the runtime.

Static/dynamic describes reception only. It must not determine whether execution
is synchronous, whether a module has workers, how it is started, or whether it
can be hot-swapped. Those are separate concerns.

The runtime remains the only mediator: modules publish declarations or messages
to it, and it routes them to compatible consumers. No provider receives a direct
reference to a concrete consumer. Interfaces continue to define mandatory
category behavior; typed contributions define optional behavior. Existing
frontend controls are the first narrow example, not yet the general router.
