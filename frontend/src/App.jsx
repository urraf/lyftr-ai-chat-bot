import { useEffect, useMemo, useRef, useState } from "react";
import { newSessionId, sendChat } from "./api.js";

const SIMULATION_PRESETS = [
  { label: "No simulated failure", value: "" },
  { label: "key_1 → 429 (rate limit)", value: "key_1=429" },
  { label: "key_1 → 500 (server error)", value: "key_1=500" },
  { label: "key_1 → 503", value: "key_1=503" },
  { label: "All keys → 429 (expect 503)", value: "*=429" },
];

const timeNow = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

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
      <span className="chip" title="Key that answered (null on cache hits)">
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

const SendIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
    <path fill="currentColor" d="M3.4 20.4 20.85 12.9a1 1 0 0 0 0-1.8L3.4 3.6a.99.99 0 0 0-1.39.91L2 9.12c0 .5.37.93.87.99L17 12 2.87 13.88c-.5.07-.87.5-.87 1l.01 4.61c0 .71.73 1.2 1.39.91z" />
  </svg>
);

const SmileIcon = () => (
  <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
    <path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18.2A8.2 8.2 0 1 1 12 3.8a8.2 8.2 0 0 1 0 16.4zM8.6 10.8a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8zm6.8 0a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8zM12 17.3c2.2 0 4.1-1.4 4.8-3.4H7.2c.7 2 2.6 3.4 4.8 3.4z" />
  </svg>
);

const PlusIcon = () => (
  <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
    <path fill="currentColor" d="M19 11h-6V5a1 1 0 0 0-2 0v6H5a1 1 0 0 0 0 2h6v6a1 1 0 0 0 2 0v-6h6a1 1 0 0 0 0-2z" />
  </svg>
);

const SearchIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
    <path fill="currentColor" d="M15.5 14h-.8l-.3-.3A6.5 6.5 0 1 0 14 15.5l.3.3v.8l5 5 1.5-1.5-5-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z" />
  </svg>
);

const NewChatIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
    <path fill="currentColor" d="M19.005 3.175H4.674C3.642 3.175 3 3.789 3 4.821V21.02l3.544-3.514h12.461c1.033 0 2.064-1.06 2.064-2.093V4.821c-.001-1.032-1.032-1.646-2.064-1.646zm-4.989 9.869H7.041V11.1h6.975v1.944zm3-4H7.041V7.1h9.975v1.944z" />
  </svg>
);

const MenuIcon = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
    <path fill="currentColor" d="M12 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm0 2a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm0 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4z" />
  </svg>
);

export default function App() {
  const sessionId = useMemo(() => newSessionId(), []); // new session on every page load
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [sending, setSending] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [slow, setSlow] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending]);

  async function handleSend(e) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setMessages((m) => [...m, { id: crypto.randomUUID?.() ?? Date.now(), role: "user", content: text, time: timeNow() }]);
    setSending(true);
    const slowTimer = setTimeout(() => setSlow(true), 8000);
    try {
      const data = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure: simulate });
      setMessages((m) => [
        ...m,
        { id: `${Date.now()}-a`, role: "assistant", content: data.reply, meta: data.metadata, time: timeNow() },
      ]);
    } catch (err) {
      setMessages((m) => [...m, { id: `${Date.now()}-e`, role: "error", content: err.message, time: timeNow() }]);
    } finally {
      clearTimeout(slowTimer);
      setSlow(false);
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

  const lastMessage = messages.length ? messages[messages.length - 1] : null;

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
    <div className="shell">
      <div className="app">
        <aside className="sidebar">
          <header className="side-head">
            <div className="me" aria-hidden="true">You</div>
            <div className="side-actions">
              <button className="icon-btn" title="New chat" aria-label="New chat" onClick={() => window.location.reload()}>
                <NewChatIcon />
              </button>
            </div>
          </header>
          <div className="search">
            <SearchIcon />
            <span>Search or start a new chat</span>
          </div>
          <div className="chat-list">
            <div className="chat-item active">
              <img className="avatar" src="/lyftr-logo.png" alt="" width="49" height="49" />
              <div className="chat-item-body">
                <div className="chat-item-top">
                  <span className="chat-name">Lyftr AI</span>
                  <span className="chat-time">{lastMessage ? lastMessage.time : ""}</span>
                </div>
                <div className="chat-preview">
                  {sending ? <em className="typing-text">typing…</em> : lastMessage ? lastMessage.content : "Ask me anything"}
                </div>
              </div>
            </div>
          </div>
          <p className="side-foot">
            LiteLLM gateway · key failover · response cache
          </p>
        </aside>

        <section className="pane">
        <header className="topbar">
          <img className="avatar" src="/lyftr-logo.png" alt="Lyftr AI" width="40" height="40" />
          <div className="who">
            <h1>Lyftr AI</h1>
            <p className="presence">{sending ? "typing…" : "online"}</p>
          </div>
          <button
            className="icon-btn"
            onClick={() => setShowSettings((s) => !s)}
            aria-expanded={showSettings}
            aria-label="Chat settings"
            title="Chat settings"
          >
            <MenuIcon />
          </button>
        </header>

        {showSettings && (
          <section className="settings">
            <label>
              <span>System prompt for this chat</span>
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
                Failover demo (sends <code>X-Simulate-Failure</code>)
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
          <div className="day-pill">Today</div>
          <div className="notice">
            🔒 Each reply shows its source, model, latency and token usage. A new chat starts every time you reload.
          </div>

          {messages.map((m) =>
            m.role === "assistant" ? (
              <div key={m.id} className="row in">
                <div className="bubble assistant" data-testid="assistant-message">
                  <div className="text">
                    <FormattedText text={m.content} />
                  </div>
                  <Meta meta={m.meta} />
                  <span className="stamp">{m.time}</span>
                </div>
              </div>
            ) : m.role === "user" ? (
              <div key={m.id} className="row out">
                <div className="bubble user">
                  <div className="text">{m.content}</div>
                  <span className="stamp">
                    {m.time} <span className="ticks">✓✓</span>
                  </span>
                </div>
              </div>
            ) : (
              <div key={m.id} className="row center">
                <div className="bubble error" role="alert">
                  <div className="text">⚠ {m.content}</div>
                </div>
              </div>
            )
          )}

          {sending && (
            <div className="row in">
              <div className="bubble assistant typing">
                <span className="dot" />
                <span className="dot" />
                <span className="dot" />
                {slow && <em className="slow">Still working… the server may be waking up (free hosting), this can take ~30 s.</em>}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </main>

        <form className="composer" onSubmit={handleSend}>
          {simulate && <div className="sim-banner">Simulating failure: {simulate}</div>}
          <div className="composer-inner">
            <span className="composer-icon" aria-hidden="true">
              <SmileIcon />
            </span>
            <span className="composer-icon" aria-hidden="true">
              <PlusIcon />
            </span>
            <textarea
              ref={inputRef}
              data-testid="chat-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Type a message"
              rows={1}
              maxLength={4000}
              autoFocus
            />
            <button
              type="submit"
              className="send"
              data-testid="send-button"
              disabled={sending || !input.trim()}
              aria-label="Send"
              title="Send"
            >
              <SendIcon />
            </button>
          </div>
        </form>
        </section>
      </div>
    </div>
  );
}
