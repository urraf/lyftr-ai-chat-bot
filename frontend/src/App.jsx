import { useEffect, useMemo, useRef, useState } from "react";
import { API_BASE_URL, fetchHealth, newSessionId, sendChat } from "./api.js";

const SIMULATION_PRESETS = [
  { label: "No simulated failure", value: "" },
  { label: "key_1 → 429 (rate limit)", value: "key_1=429" },
  { label: "key_1 → 500 (server error)", value: "key_1=500" },
  { label: "key_1 → 503", value: "key_1=503" },
  { label: "All keys → 429 (expect 503)", value: "*=429" },
];

// Minimal, XSS-safe formatting for model output: **bold**, `code` and ``` blocks.
// Builds React elements (no innerHTML), everything else stays plain text.
function renderInline(text, keyPrefix) {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part, i) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) return <strong key={`${keyPrefix}-${i}`}>{part.slice(2, -2)}</strong>;
    if (/^`[^`]+`$/.test(part)) return <code key={`${keyPrefix}-${i}`}>{part.slice(1, -1)}</code>;
    return part;
  });
}

function FormattedText({ text }) {
  const blocks = text.split(/```[\w-]*\n?/);
  return blocks.map((block, i) =>
    i % 2 === 1 ? (
      <pre key={i} className="codeblock">
        <code>{block.replace(/\n$/, "")}</code>
      </pre>
    ) : (
      <span key={i}>{renderInline(block, i)}</span>
    )
  );
}

function Meta({ meta }) {
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta">
      <span className={`chip ${cached ? "chip-cache" : "chip-live"}`} title="Resolution source">
        <span data-testid="meta-source">{meta.resolution_source}</span>
      </span>
      <span className="chip" title="Model used">
        <span data-testid="meta-model">{meta.model}</span>
      </span>
      <span className="chip" title="Server-measured latency">
        <span data-testid="meta-latency">{meta.latency_ms} ms</span>
      </span>
      <span className="chip" title="Input (prompt) tokens for this request">
        in <span data-testid="meta-input-tokens">{meta.input_tokens}</span>
      </span>
      <span className="chip" title="Output (completion) tokens for this reply">
        out <span data-testid="meta-output-tokens">{meta.output_tokens}</span>
      </span>
      <span className="chip chip-muted" title="Key that answered (null on cache hits)">
        key {meta.key_id ?? "—"}
      </span>
      {meta.fallback_attempts > 0 && (
        <span className="chip chip-warn" title="Keys that failed before this one succeeded">
          failover ×{meta.fallback_attempts}
        </span>
      )}
    </div>
  );
}

export default function App() {
  const sessionId = useMemo(() => newSessionId(), []); // new session on every page load
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [sending, setSending] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [health, setHealth] = useState(null);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth({ status: "unreachable" }));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending]);

  async function handleSend(e) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setMessages((m) => [...m, { id: crypto.randomUUID?.() ?? Date.now(), role: "user", content: text }]);
    setSending(true);
    try {
      const data = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure: simulate });
      setMessages((m) => [...m, { id: `${Date.now()}-a`, role: "assistant", content: data.reply, meta: data.metadata }]);
    } catch (err) {
      setMessages((m) => [...m, { id: `${Date.now()}-e`, role: "error", content: err.message }]);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  const totals = messages.reduce(
    (acc, m) => {
      if (m.meta) {
        acc.replies += 1;
        if (m.meta.resolution_source === "LLM_CACHE") acc.cached += 1;
      }
      return acc;
    },
    { replies: 0, cached: 0 }
  );

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="logo">◆</span>
          <div>
            <h1>Lyftr Chat</h1>
            <p className="sub">
              LiteLLM gateway · key failover · response cache
            </p>
          </div>
        </div>
        <div className="header-right">
          <span className={`status ${health?.status === "ok" ? "ok" : "bad"}`} title={API_BASE_URL}>
            {health == null
              ? "checking…"
              : health.status === "ok"
              ? `backend ok · ${health.keys_configured} keys · cache ${health.cache_enabled ? "on" : "off"}`
              : "backend unreachable"}
          </span>
          <button className="ghost" onClick={() => setShowSettings((s) => !s)} aria-expanded={showSettings}>
            {showSettings ? "Hide settings" : "Settings"}
          </button>
        </div>
      </header>

      {showSettings && (
        <section className="settings">
          <label>
            <span>System prompt (this session)</span>
            <textarea
              data-testid="system-prompt-input"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder="e.g. You are a pirate. Answer in one sentence."
              rows={2}
              maxLength={4000}
            />
          </label>
          <label>
            <span>
              Failover demo — sends <code>X-Simulate-Failure</code> with the next messages
            </span>
            <select value={simulate} onChange={(e) => setSimulate(e.target.value)}>
              {SIMULATION_PRESETS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <p className="session">
            Session <code>{sessionId}</code> · {totals.replies} replies · {totals.cached} from cache
          </p>
        </section>
      )}

      <main className="chat">
        {messages.length === 0 && (
          <div className="empty">
            <h2>Start a conversation</h2>
            <p>Every reply shows where it came from, which model answered, latency and token usage for that request.</p>
            <p className="hint">Tip: send the same first message from a new tab to see an <b>LLM_CACHE</b> hit.</p>
          </div>
        )}
        {messages.map((m) =>
          m.role === "assistant" ? (
            <div key={m.id} className="row left">
              <div className="bubble assistant" data-testid="assistant-message">
                <div className="text">
                  <FormattedText text={m.content} />
                </div>
                <Meta meta={m.meta} />
              </div>
            </div>
          ) : m.role === "user" ? (
            <div key={m.id} className="row right">
              <div className="bubble user">
                <div className="text">{m.content}</div>
              </div>
            </div>
          ) : (
            <div key={m.id} className="row left">
              <div className="bubble error" role="alert">
                <div className="text">⚠ {m.content}</div>
              </div>
            </div>
          )
        )}
        {sending && (
          <div className="row left">
            <div className="bubble assistant typing">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <form className="composer" onSubmit={handleSend}>
        {simulate && <div className="sim-banner">Simulating: {simulate}</div>}
        <div className="composer-inner">
          <textarea
            ref={inputRef}
            data-testid="chat-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Type a message…"
            rows={1}
            maxLength={4000}
            autoFocus
          />
          <button type="submit" data-testid="send-button" disabled={sending || !input.trim()}>
            {sending ? "…" : "Send"}
          </button>
        </div>
      </form>
    </div>
  );
}
