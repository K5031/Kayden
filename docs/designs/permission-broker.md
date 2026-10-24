# Permission broker

**Status:** Implemented as cooperative policy enforcement, not sandboxing.

## Purpose

The model may propose an operation, but the runtime decides whether it is allowed
to execute. The same authorization boundary also applies to user-facing controls
so Kayden does not develop separate policy paths for model and frontend actions.

The broker separates:

- **operation definition:** stable ID, label, effects, default decision, preview;
- **policy:** allow, ask, or deny for an exact operation ID;
- **invocation context:** trusted origin and whether consent was explicit;
- **arguments:** the exact proposed target and values.

Effects classify behavior for explanation and future policy. They do not choose
the decision by themselves.

## Authorization flow

```text
operation + arguments + runtime-assigned context
                       |
                       v
                permission broker
                /       |       \
             allow     ask      deny
               |        |         |
               |   frontend UI    +--> reject
               |        |
               +--- authorized ---> execute
```

Rules in configuration override an operation's declared default. With `ask`, the
runtime creates a request containing a deep copy of the arguments and sends it to
the active frontend. The frontend returns allow-once or deny. If there is no
question handler, `ask` denies rather than silently allowing.

The runtime assigns invocation origin; a module cannot claim to be the user.
`origin="user"` and `explicit=True` describe the interaction but do not grant a
universal bypass. A normal mute click can have an allow-by-default operation,
while a dangerous click may still ask.

## Enforcement points

Model tool calls are authorized inside `ToolRuntime.execute`, immediately before
the provider's `execute` method. Modules may supply argument-specific operation
metadata and previews; otherwise the runtime creates a generic `tool.<name>`
operation.

Contributed controls are authorized by the application runtime immediately
before invoking the provider callback. This keeps frontend presentation separate
from authorization.

Filesystem write operations can provide a diff or text preview so the permission
dialog shows the proposed change rather than only its arguments. Approval applies
to that one invocation; it does not authorize another path or later operation.

Long-term-memory forgetting is classified as `memory.delete` but intentionally
defaults to allow. Classification and policy remain independent.

## Invariants

- Operation IDs are namespaced and policy rules match exact IDs.
- Only the runtime creates trusted invocation context.
- Authorization occurs at the actual execution boundary.
- An `ask` decision without an available frontend denies.
- Arguments shown to an asker are copied before presentation.
- A denial prevents the provider callback or tool execution.
- Previews are provider-generated descriptions, not evidence that an operation
  is safe.
- User origin alone never implies authorization for all downstream effects.

## Security boundary and trade-offs

The broker governs operations that voluntarily pass through it. Kayden modules
currently execute as unrestricted Python in the same process, so a malicious or
careless module can bypass the broker and access the operating system directly.
Operation metadata can also lie.

That is acceptable for trusted built-in modules and useful as a consistent user
policy layer, but it is not sufficient for an untrusted marketplace. Third-party
code will eventually require a separate sandbox or restricted host process. The
permission broker should remain the user-intent layer even after such an
enforcement layer exists.
