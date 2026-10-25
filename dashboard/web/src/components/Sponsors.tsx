import { LogoCloud, type Logo } from "@/components/ui/logo-cloud-2";

// The Cal Hacks 13.0 sponsors whose tools BackHouse runs on, each with the job it does here. The logos are saved in
// public/sponsors: Capital One's from Wikimedia Commons, Fetch.ai's and ASI:One's from fetch.ai.
const SPONSORS: Logo[] = [
  {
    src: "/sponsors/capital-one.svg",
    alt: "Capital One",
    width: 418,
    height: 150,
    className: "h-11",
    name: "The bank",
    detail: "Every check reads Demo Bistro's bank from Nessie, Capital One's hackathon banking API.",
  },
  {
    src: "/sponsors/fetch-ai.svg",
    alt: "Fetch.ai",
    width: 122,
    height: 20,
    className: "h-6",
    name: "The agents",
    detail: "The Bookkeeper, its Verifier and the Market scout are agents built with Fetch.ai's uAgents.",
  },
  {
    src: "/sponsors/asi-one.svg",
    alt: "ASI:One",
    width: 107,
    height: 24,
    className: "h-7",
    name: "The AI model",
    detail: "The Chat, the Bookkeeper and the Market scout run on ASI:One, Fetch.ai's AI model.",
  },
];

export function Sponsors() {
  return (
    // The cloud's lines run out to the page edges; clipping keeps them from adding a sideways scroll.
    <section aria-labelledby="sponsors-heading" className="overflow-x-clip pb-20 pt-8">
      <div className="mx-auto max-w-5xl px-4 sm:px-6 2xl:max-w-6xl">
        <h2 id="sponsors-heading" className="text-center font-display text-3xl font-extrabold text-ink [font-stretch:80%]">
          Built with our sponsors' tools
        </h2>
        <p className="mx-auto mt-2 max-w-[56ch] text-center text-[1.0625rem] text-pencil">
          Capital One and Fetch.ai sponsor Cal Hacks 13.0. Here's where their tools run in BackHouse.
        </p>
        <LogoCloud logos={SPONSORS} className="mt-10" />
      </div>
    </section>
  );
}
