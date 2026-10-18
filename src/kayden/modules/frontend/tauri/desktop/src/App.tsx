import { invoke } from "@tauri-apps/api/core";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type BridgeConnection = {
  mode: "gui" | "floaty";
};

type FrontendConfig = {
  mode: "gui" | "floaty";
  title: string;
  floating: boolean;
  assistantName: string;
  capture: boolean;
  captureLabel: string;
  models: ModelOption[];
  selectedModel: string | null;
};

type ModelOption = {
  id: string;
  name: string;
};

type BridgeEvent = {
  id?: number;
  type: string;
  message?: string;
  text?: string;
  interrupted?: boolean;
  state?: "idle" | "recording" | "transcribing";
  model?: string;
  label?: string;
};

type EventBatch = {
  events: BridgeEvent[];
};

type Message = {
  id: number;
  role: "user" | "assistant";
  content: string;
  interrupted?: boolean;
  tools?: ToolActivity[];
};

type ToolActivity = {
  id: number;
  name: string;
  detail?: string;
  state: "running" | "finished" | "failed";
};

type ParsedToolStatus = Omit<ToolActivity, "id">;

const starter: Message[] = [
  {
    id: 0,
    role: "assistant",
    content: "Ready when you are.",
  },
];

function Logo({ name }: { name: string }) {
  return (
    <div className="logo" aria-label={name}>
      <span className="logo-name">{name}</span>
    </div>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 12h15M13 6l6 6-6 6" />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="7" y="7" width="10" height="10" rx="2" />
    </svg>
  );
}

function DownIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="8" y="8" width="10" height="10" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </svg>
  );
}

function parseToolStatus(message: string): ParsedToolStatus | null {
  if (!message.startsWith("[tool]")) return null;
  const body = message.replace(/^\[tool\]\s*/, "");
  const failure = body.match(/^([^\s(]+) failed:\s*([\s\S]*)$/);
  if (failure) {
    return { name: failure[1], detail: failure[2], state: "failed" };
  }
  const finished = body.match(/^([^\s(]+) finished$/);
  if (finished) {
    return { name: finished[1], state: "finished" };
  }
  const started = body.match(/^([^\s(]+)(?:\(([\s\S]*)\))?$/);
  if (!started) return null;
  return {
    name: started[1],
    detail: started[2] && started[2] !== "{}" ? started[2] : undefined,
    state: "running",
  };
}

function ToolGroup({
  tools,
  expanded,
  onToggle,
}: {
  tools: ToolActivity[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const running = tools.some((tool) => tool.state === "running");
  const failed = tools.some((tool) => tool.state === "failed");
  const open = running || expanded;
  const label = running
    ? tools.length === 1
      ? `Using ${tools[0].name}`
      : `Using tools (${tools.length})`
    : failed
      ? `Tool run finished with an error`
      : `Used ${tools.length} ${tools.length === 1 ? "tool" : "tools"}`;

  return (
    <section className={`tool-group ${failed ? "has-error" : ""}`}>
      <button className="tool-summary" type="button" onClick={onToggle} aria-expanded={open}>
        <span className={`tool-state ${running ? "running" : failed ? "failed" : "finished"}`} />
        <span>{label}</span>
        <DownIcon />
      </button>
      {open && (
        <div className="tool-list">
          {tools.map((tool) => (
            <div className="tool-row" key={tool.id}>
              <span className={`tool-row-state ${tool.state}`} />
              <div>
                <strong>{tool.name}</strong>
                {tool.detail && <pre>{tool.detail}</pre>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [connection, setConnection] = useState<BridgeConnection | null>(null);
  const [config, setConfig] = useState<FrontendConfig | null>(null);
  const [messages, setMessages] = useState<Message[]>(starter);
  const [composer, setComposer] = useState("");
  const [status, setStatus] = useState("Connecting…");
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [captureState, setCaptureState] = useState("idle");
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [followingLatest, setFollowingLatest] = useState(true);
  const [expandedTools, setExpandedTools] = useState<Set<number>>(() => new Set());
  const [elapsed, setElapsed] = useState(0);
  const [copiedMessage, setCopiedMessage] = useState<number | null>(null);
  const nextMessageId = useRef(1);
  const nextToolId = useRef(1);
  const assistantId = useRef<number | null>(null);
  const startedAt = useRef<number | null>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const assistantName = config?.assistantName || "Kayden";
  const modelLabel = config?.models.find((model) => model.id === selectedModel)?.name
    || selectedModel;

  const request = async (path: string, body: object = {}) => {
    if (!connection) return;
    await invoke("bridge_command", { path, body });
  };

  const applyEvent = (event: BridgeEvent) => {
    if (event.type === "user" && event.message) {
      assistantId.current = null;
      setMessages((current) => [
        ...(current.length === 1 && current[0].id === 0 ? [] : current),
        { id: nextMessageId.current++, role: "user", content: event.message! },
      ]);
      startedAt.current = Date.now();
      setElapsed(0);
      setBusy(true);
      setStatus("Thinking…");
      setFollowingLatest(true);
      return;
    }
    if (event.type === "assistant" && event.text !== undefined) {
      const text = event.text;
      const activeAssistant = assistantId.current;
      if (activeAssistant !== null) {
        setMessages((current) =>
          current.map((message) =>
            message.id === activeAssistant
              ? { ...message, content: message.content + text }
              : message,
          ),
        );
      } else {
        const id = nextMessageId.current++;
        assistantId.current = id;
        setMessages((current) => [
          ...current,
          { id, role: "assistant", content: text },
        ]);
      }
      return;
    }
    if (event.type === "status" && event.message) {
      if (event.message.startsWith("Model: ")) {
        setSelectedModel(event.message.slice("Model: ".length));
        setStatus("Ready");
        return;
      }
      const tool = parseToolStatus(event.message);
      if (tool) {
        let activeAssistant = assistantId.current;
        if (activeAssistant === null) {
          activeAssistant = nextMessageId.current++;
          assistantId.current = activeAssistant;
          const activity = { ...tool, id: nextToolId.current++ };
          setMessages((current) => [
            ...current,
            { id: activeAssistant!, role: "assistant", content: "", tools: [activity] },
          ]);
        } else {
          const messageId = activeAssistant;
          const activityId = nextToolId.current++;
          setMessages((current) =>
            current.map((message) => {
              if (message.id !== messageId) return message;
              const tools = message.tools || [];
              if (tool.state === "running") {
                return { ...message, tools: [...tools, { ...tool, id: activityId }] };
              }
              let index = -1;
              for (let candidate = tools.length - 1; candidate >= 0; candidate -= 1) {
                if (tools[candidate].name === tool.name && tools[candidate].state === "running") {
                  index = candidate;
                  break;
                }
              }
              if (index < 0) {
                return { ...message, tools: [...tools, { ...tool, id: activityId }] };
              }
              return {
                ...message,
                tools: tools.map((activity, activityIndex) =>
                  activityIndex === index
                    ? {
                        ...activity,
                        state: tool.state,
                        detail: tool.detail || activity.detail,
                      }
                    : activity,
                ),
              };
            }),
          );
        }
        setStatus(
          tool.state === "running"
            ? `Using ${tool.name}…`
            : tool.state === "failed"
              ? `${tool.name} failed`
              : "Thinking…",
        );
        return;
      }
      setStatus(event.message);
      return;
    }
    if (event.type === "finish") {
      const completedId = assistantId.current;
      if (event.interrupted && completedId !== null) {
        setMessages((current) =>
          current.map((message) =>
            message.id === completedId ? { ...message, interrupted: true } : message,
          ),
        );
      }
      assistantId.current = null;
      startedAt.current = null;
      setBusy(false);
      setStatus(event.interrupted ? "Interrupted" : "Ready");
      return;
    }
    if (event.type === "capture_state" && event.state) {
      setCaptureState(event.state);
      setStatus(
        event.state === "recording"
          ? "Recording…"
          : event.state === "transcribing"
            ? "Transcribing…"
            : "Ready",
      );
      return;
    }
    if (event.type === "draft" && event.text) {
      setComposer((current) => `${current}${current.trim() ? " " : ""}${event.text}`);
      return;
    }
    if (event.type === "model" && event.model) {
      setSelectedModel(event.model);
      setStatus("Ready");
    }
  };

  useEffect(() => {
    let cancelled = false;

    const connect = async () => {
      try {
        const details = await invoke<BridgeConnection>("bridge_config");
        if (cancelled) return;
        setConnection(details);
        document.body.dataset.mode = details.mode;
        const frontendConfig = await invoke<FrontendConfig>("bridge_frontend_config");
        setConfig(frontendConfig);
        setSelectedModel(frontendConfig.selectedModel);
        setConnectionError(null);

        let lastEvent = 0;
        while (!cancelled) {
          const batch = await invoke<EventBatch>("bridge_events", { lastEvent });
          if (cancelled) return;
          if (!Array.isArray(batch.events)) {
            throw new Error("Kayden returned an invalid event batch");
          }
          for (const event of batch.events) {
            if (event.id && event.id <= lastEvent) continue;
            if (event.id) lastEvent = event.id;
            applyEvent(event);
          }
        }
      } catch (error) {
        if (!cancelled) {
          setConnectionError(error instanceof Error ? error.message : String(error));
          setStatus("Disconnected");
        }
      }
    };

    void connect();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!followingLatest) return;
    const frame = requestAnimationFrame(() => {
      transcript.current?.scrollTo({ top: transcript.current.scrollHeight });
    });
    return () => cancelAnimationFrame(frame);
  }, [messages, followingLatest]);

  useEffect(() => {
    if (!busy) return;
    const update = () => {
      if (startedAt.current !== null) {
        setElapsed(Math.floor((Date.now() - startedAt.current) / 1000));
      }
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    const handleGlobalKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        if (captureState === "recording") {
          void request("/commands/capture/cancel");
        } else if (busy) {
          void request("/commands/interrupt");
        }
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        composerInput.current?.focus();
      }
    };
    window.addEventListener("keydown", handleGlobalKey);
    return () => window.removeEventListener("keydown", handleGlobalKey);
  }, [busy, captureState, connection]);

  const updateScrollState = () => {
    const view = transcript.current;
    if (!view) return;
    const distance = view.scrollHeight - view.scrollTop - view.clientHeight;
    setFollowingLatest(distance < 72);
  };

  const jumpToLatest = () => {
    setFollowingLatest(true);
    transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: "smooth" });
  };

  const toggleTools = (messageId: number) => {
    setExpandedTools((current) => {
      const next = new Set(current);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      return next;
    });
  };

  const copyMessage = async (message: Message) => {
    if (!message.content) return;
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedMessage(message.id);
      window.setTimeout(() => setCopiedMessage((current) => current === message.id ? null : current), 1400);
    } catch {
      setStatus("Could not copy response");
    }
  };

  const changeModel = async (model: string) => {
    if (!model || model === selectedModel || busy) return;
    const previous = selectedModel;
    setSelectedModel(model);
    setStatus("Switching model…");
    try {
      await request("/commands/model", { model });
      setConnectionError(null);
    } catch (error) {
      setSelectedModel(previous);
      setConnectionError(error instanceof Error ? error.message : String(error));
      setStatus("Model switch failed");
    }
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const message = composer.trim();
    if (!message || !connection) return;
    setComposer("");
    try {
      await request("/commands/submit", { message });
    } catch (error) {
      setComposer(message);
      setConnectionError(error instanceof Error ? error.message : String(error));
    }
  };

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const toggleCapture = () => {
    if (captureState === "recording") {
      void request("/commands/capture/finish");
    } else if (captureState === "idle") {
      void request("/commands/capture/start");
    }
  };

  return (
    <main className={config?.mode === "floaty" ? "app app-floaty" : "app"}>
      <section className="conversation">
        <header className="topbar">
          <div className="topbar-logo"><Logo name={assistantName} /></div>
          <div>
            <h1>{config?.mode === "floaty" ? assistantName : "Conversation"}</h1>
            <p>
              <span className={connectionError ? "status-dot error" : "status-dot"} />
              {connectionError || status}
            </p>
          </div>
          {modelLabel && config && config.models.length > 1 ? (
            <select
              className="model-chip model-select"
              aria-label="Model"
              value={selectedModel || ""}
              disabled={busy}
              onChange={(event) => void changeModel(event.target.value)}
            >
              {config.models.map((model) => (
                <option value={model.id} key={model.id}>{model.name}</option>
              ))}
            </select>
          ) : modelLabel ? (
            <span className="model-chip">{modelLabel}</span>
          ) : null}
        </header>

        <div className="transcript-shell">
          <div className="transcript" ref={transcript} onScroll={updateScrollState}>
            <div className="message-list">
              {messages.map((message) => (
                <article className={`message ${message.role}`} key={message.id}>
                  <div className="message-body">
                    {message.tools && message.tools.length > 0 && (
                      <ToolGroup
                        tools={message.tools}
                        expanded={expandedTools.has(message.id)}
                        onToggle={() => toggleTools(message.id)}
                      />
                    )}
                    {message.role === "assistant" ? (
                      message.content && <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                    ) : (
                      <p>{message.content}</p>
                    )}
                    {message.interrupted && <span className="interrupted">Interrupted</span>}
                    {message.role === "assistant" && message.content && message.id !== 0 && (
                      <button className="copy-message" type="button" onClick={() => void copyMessage(message)}>
                        <CopyIcon /> {copiedMessage === message.id ? "Copied" : "Copy"}
                      </button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </div>
          {!followingLatest && (
            <button className="jump-latest" type="button" onClick={jumpToLatest}>
              <DownIcon /> Latest
            </button>
          )}
        </div>

        <div className="composer-zone">
          {busy && (
            <div className="run-status">
              <span className="run-spinner" />
              <span className="run-label">{status}</span>
              <time>{elapsed}s</time>
              <button type="button" onClick={() => void request("/commands/interrupt")}>
                <StopIcon /> Stop
              </button>
            </div>
          )}
          <form className="composer-shell" onSubmit={submit}>
            <textarea
              ref={composerInput}
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
              onKeyDown={onComposerKey}
              placeholder={`Message ${assistantName}…`}
              rows={config?.mode === "floaty" ? 1 : 2}
              autoFocus
            />
            <div className="composer-actions">
              {config?.capture && (
                <button
                  className={`icon-button mic ${captureState === "recording" ? "recording" : ""}`}
                  type="button"
                  onClick={toggleCapture}
                  disabled={captureState === "transcribing"}
                  title={captureState === "recording" ? "Stop recording" : config.captureLabel}
                >
                  <MicIcon />
                </button>
              )}
              <button
                className="send-button"
                type="submit"
                disabled={!composer.trim() || !connection}
                title={busy ? "Interrupt the current response and send" : "Send message"}
              >
                <SendIcon />
              </button>
            </div>
          </form>
          <footer>
            {busy ? "Enter to interrupt & send · Esc to stop" : "Enter to send · Shift+Enter for a new line · Ctrl+K to focus"}
          </footer>
        </div>
      </section>
    </main>
  );
}
