import type { Route } from "./App";

/** The site's pages, in menu order. The header's menus and the footer both list these. */
export const LINKS: { route: Route; href: string; label: string; detail: string }[] = [
  { route: "dashboard", href: "#/", label: "Dashboard", detail: "Your cash at a glance" },
  { route: "chat", href: "#/chat", label: "Chat", detail: "Ask about your books" },
  { route: "how", href: "#/how-it-works", label: "How it works", detail: "The agents behind the numbers" },
  { route: "developer", href: "#/developer", label: "Developer", detail: "Which agent calls which, live" },
];
