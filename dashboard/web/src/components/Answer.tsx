import { Fragment } from "react";

/** **bold** inside a line; everything else stays plain text (never injected as HTML). */
function inline(text: string) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
      <strong key={i} className="font-bold">
        {part.slice(2, -2)}
      </strong>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  );
}

type Block = { kind: "p"; text: string } | { kind: "ul" | "ol"; items: string[] };

/** The few bits of markdown the chat uses: paragraphs, bullet and numbered lists, bold. */
function blocks(text: string): Block[] {
  const out: Block[] = [];
  let gap = true;
  for (const raw of text.split("\n")) {
    const line = raw.trim().replace(/^#{1,6}\s+/, "");
    if (!line) {
      gap = true;
      continue;
    }
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    const numbered = line.match(/^\d+[.)]\s+(.*)$/);
    const last = out[out.length - 1];
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      const item = (bullet ?? numbered)![1];
      if (last && last.kind === kind) last.items.push(item);
      else out.push({ kind, items: [item] });
    } else if (last && last.kind === "p" && !gap) {
      last.text += ` ${line}`;
    } else {
      out.push({ kind: "p", text: line });
    }
    gap = false;
  }
  return out;
}

export function Answer({ text }: { text: string }) {
  return (
    <div className="space-y-3 text-[1.0625rem] leading-relaxed text-carbon">
      {blocks(text).map((b, i) =>
        b.kind === "p" ? (
          <p key={i}>{inline(b.text)}</p>
        ) : b.kind === "ul" ? (
          <ul key={i} className="list-disc space-y-1 pl-5 marker:text-ink">
            {b.items.map((item, j) => (
              <li key={j}>{inline(item)}</li>
            ))}
          </ul>
        ) : (
          <ol key={i} className="list-decimal space-y-1 pl-5 marker:font-bold marker:text-ink">
            {b.items.map((item, j) => (
              <li key={j}>{inline(item)}</li>
            ))}
          </ol>
        ),
      )}
    </div>
  );
}
