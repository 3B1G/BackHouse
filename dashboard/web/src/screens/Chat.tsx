import { useEffect, useId, useRef, useState } from "react";
import { askChat, recall, remember, type ChatTurn } from "../api";
import { Answer } from "../components/Answer";
import { CheckMark } from "../components/Header";

type Message = ChatTurn & { sources?: string[]; failed?: boolean; offTopic?: boolean };

const SUGGESTIONS = [
  "When do we run out of cash?",
  "Which bills are still unpaid?",
  "What did we pay Sonoma Dairy Supply in September?",
  "What if January has 15% fewer guests?",
];

function load(): Message[] {
  try {
    return JSON.parse(recall("boh-chat") ?? "[]") as Message[];
  } catch {
    return [];
  }
}

/** "the forecast, your records (SDS-CHS-10105) and the bank (Nessie)" */
function sourcesText(sources: string[]) {
  const ids = sources.filter((s) => s.startsWith("Your records: ")).map((s) => s.slice(14));
  const parts: string[] = [];
  if (sources.includes("Forecast")) parts.push("the forecast");
  if (ids.length) parts.push(`your records (${ids.join(", ")})`);
  else if (sources.includes("Your records")) parts.push("your records");
  if (sources.includes("Bank (Nessie)")) parts.push("the bank (Nessie)");
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0];
}

function Spinner() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6 shrink-0 animate-spin text-ink [animation-duration:1.4s]" fill="none" strokeWidth="2.5">
      <circle cx="12" cy="12" r="9.5" stroke="var(--color-rule)" />
      <path d="M12 2.5a9.5 9.5 0 0 1 9.5 9.5" stroke="currentColor" strokeLinecap="round" />
    </svg>
  );
}

function Tick() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="mt-1 h-4 w-4 shrink-0 text-ink" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
  );
}

export function Chat({ restaurant }: { restaurant: string | null }) {
  const inputId = useId();
  const [messages, setMessages] = useState<Message[]>(load);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState<string[]>([]);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const asked = useRef(false);

  useEffect(() => remember("boh-chat", JSON.stringify(messages)), [messages]);

  useEffect(() => {
    if (!messages.length && !busy) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    end.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
  }, [messages, steps, busy]);

  const send = async (text: string, earlier: Message[] = messages) => {
    const question = text.trim();
    if (!question || busy) return;
    const asked: Message = { role: "user", content: question };
    // Declined questions and their replies stay on screen but never go back to the model, so they can't steer later answers.
    const kept = earlier.filter((m, i) => !m.failed && !m.offTopic && !(m.role === "user" && earlier[i + 1]?.offTopic));
    const history: ChatTurn[] = [...kept, asked].map(({ role, content }) => ({ role, content }));
    setMessages([...earlier, asked]);
    setDraft("");
    if (input.current) input.current.style.height = "";
    setBusy(true);
    setSteps([]);
    try {
      await askChat(history, (e) => {
        if (e.type === "tool") setSteps((s) => [...s, e.text]);
        else if (e.type === "answer")
          setMessages((m) => [
            ...m,
            { role: "assistant", content: e.text || "I couldn't find an answer to that.", sources: e.sources, offTopic: e.off_topic },
          ]);
        else setMessages((m) => [...m, { role: "assistant", content: e.text, failed: true }]);
      });
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", content: (err as Error).message, failed: true }]);
    } finally {
      setBusy(false);
      setSteps([]);
      input.current?.focus();
    }
  };

  // #/chat?ask=... asks a question straight away (links from the dashboard use it).
  useEffect(() => {
    if (asked.current) return;
    asked.current = true;
    const ask = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("ask");
    if (ask) {
      window.history.replaceState(null, "", "#/chat");
      void send(ask);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const retry = () => {
    const lastUser = messages.map((m) => m.role).lastIndexOf("user");
    if (lastUser < 0) return;
    void send(messages[lastUser].content, messages.slice(0, lastUser));
  };

  const grow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  return (
    <section aria-labelledby="chat-heading" className="mx-auto flex min-h-[calc(100dvh-7rem)] max-w-3xl flex-col px-4 sm:px-6">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b border-rule pb-5 pt-8">
        <div>
          <h1 id="chat-heading" className="font-display text-[clamp(2.25rem,5vw,3.25rem)] font-extrabold leading-none text-ink [font-stretch:66%]">
            Ask about your books
          </h1>
          <p className="mt-3 max-w-[58ch] text-[0.9375rem] text-pencil">
            Answers come from {restaurant ? `${restaurant.split(",")[0]}'s` : "your"} bank and records, through ASI:One by Fetch.ai. It can
            look things up and run what-ifs, but it can't change anything.
          </p>
        </div>
        {messages.length > 0 && !busy && (
          <button
            type="button"
            onClick={() => setMessages([])}
            className="min-h-11 whitespace-nowrap rounded-md px-3 text-sm font-bold text-ink underline underline-offset-4 hover:bg-check"
          >
            New conversation
          </button>
        )}
      </div>

      <div role="log" aria-live="polite" aria-relevant="additions" className="flex-1 space-y-7 py-7">
        {messages.length === 0 && !busy && (
          <div>
            <h2 className="font-display text-2xl font-extrabold text-ink [font-stretch:80%]">Try asking</h2>
            <ul className="mt-3 border-t border-rule">
              {SUGGESTIONS.map((s) => (
                <li key={s} className="border-b border-rule">
                  <button
                    type="button"
                    onClick={() => void send(s)}
                    className="flex min-h-14 w-full items-center px-2 py-3 text-left text-[1.0625rem] text-carbon transition-colors duration-150 hover:bg-check"
                  >
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <p className="max-w-[85%] whitespace-pre-wrap rounded-lg rounded-br-sm bg-check px-4 py-3 text-[1.0625rem] text-carbon">
                <span className="sr-only">You asked: </span>
                {m.content}
              </p>
            </div>
          ) : (
            <article key={i} className="max-w-[68ch]">
              <p className="mb-1.5 flex items-center gap-2 text-sm font-bold text-ink">
                <CheckMark className="h-5 w-5 text-ink" />
                BackHouse
              </p>
              {m.failed ? (
                <div>
                  <p role="alert" className="text-[1.0625rem] font-bold text-serial">
                    {m.content}
                  </p>
                  {i === messages.length - 1 && (
                    <button
                      type="button"
                      onClick={retry}
                      className="mt-3 min-h-11 rounded-md border-2 border-ink px-4 text-[0.9375rem] font-bold text-ink hover:bg-check"
                    >
                      Try again
                    </button>
                  )}
                </div>
              ) : (
                <Answer text={m.content} />
              )}
              {!!m.sources?.length && <p className="mt-2 text-sm text-pencil">From {sourcesText(m.sources)}.</p>}
              {m.offTopic && i === messages.length - 1 && !busy && (
                <ul className="mt-3 flex flex-wrap gap-2">
                  {SUGGESTIONS.map((s) => (
                    <li key={s}>
                      <button
                        type="button"
                        onClick={() => void send(s)}
                        className="min-h-11 rounded-md border-2 border-rule bg-paper px-3 py-2 text-left text-[0.9375rem] text-carbon transition-colors duration-150 hover:border-ink"
                      >
                        {s}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </article>
          ),
        )}

        {busy && (
          <div className="flex gap-3">
            <Spinner />
            <div>
              <p className="font-bold text-ink">{steps.length ? steps[steps.length - 1] : "Reading your question"}…</p>
              {steps.length > 1 && (
                <ul className="mt-1 space-y-0.5 text-sm text-pencil">
                  {steps.slice(0, -1).map((s, i) => (
                    <li key={i} className="flex gap-1.5">
                      <Tick />
                      {s}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
        <div ref={end} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send(draft);
        }}
        className="sticky bottom-0 -mx-4 border-t border-rule bg-paper px-4 pb-4 pt-3 sm:-mx-6 sm:px-6"
      >
        <label htmlFor={inputId} className="mb-1.5 block text-sm font-bold text-ink">
          Ask about your cash, a bill or a what-if
        </label>
        <div className="flex items-end gap-2">
          <textarea
            id={inputId}
            ref={input}
            rows={1}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              grow(e.target);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send(draft);
              }
            }}
            placeholder="Type a question"
            className="min-h-12 flex-1 resize-none rounded-md border-2 border-rule bg-paper px-3 py-2.5 text-[1.0625rem] text-carbon placeholder:text-pencil focus:border-ink focus:outline-none"
          />
          <button
            type="submit"
            disabled={busy || !draft.trim()}
            className="min-h-12 rounded-md border-2 border-ink bg-ink px-5 text-base font-bold text-white transition-colors duration-150 hover:bg-[#173b29] disabled:border-rule disabled:bg-check disabled:text-pencil"
          >
            Ask
          </button>
        </div>
      </form>
    </section>
  );
}
