const runtime = (typeof window !== "undefined" && window.__APP_CONFIG__) || {};

export const API_BASE_URL = (
  runtime.API_BASE_URL ||
  import.meta.env.VITE_API_BASE_URL ||
  "http://localhost:8000"
).replace(/\/+$/, "");

export function newSessionId() {
  const rand =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(36).slice(2, 14);
  return `s-${rand}`;
}

function describeError(status, body) {
  const detail = body && body.detail;
  if (status === 503) return "All LLM providers are unavailable right now (every API key failed). Please try again in a moment.";
  if (status === 422) {
    if (Array.isArray(detail)) {
      return "Invalid request: " + detail.map((d) => `${(d.loc || []).slice(-1)[0] ?? "body"}: ${d.msg}`).join("; ");
    }
    return `Invalid request: ${typeof detail === "string" ? detail : "please check your message."}`;
  }
  if (typeof detail === "string") return `Error ${status}: ${detail}`;
  return `Unexpected error (HTTP ${status}).`;
}

export async function sendChat({ sessionId, message, systemPrompt, simulateFailure }) {
  const body = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) body.system_prompt = systemPrompt.trim();
  const headers = { "Content-Type": "application/json" };
  if (simulateFailure) headers["X-Simulate-Failure"] = simulateFailure;

  let res;
  try {
    res = await fetch(`${API_BASE_URL}/chat`, { method: "POST", headers, body: JSON.stringify(body) });
  } catch {
    throw new Error(`Cannot reach the backend at ${API_BASE_URL}. Check your connection and try again.`);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON body */
  }
  if (!res.ok) throw new Error(describeError(res.status, data));
  if (!data || typeof data.reply !== "string") throw new Error("The backend returned an unexpected response.");
  return data;
}
