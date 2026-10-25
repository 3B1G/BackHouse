import { useEffect, useRef, useState } from "react";
import { recall, remember } from "./api";
import { Footer } from "./components/Footer";
import { Header } from "./components/Header";
import { Sponsors } from "./components/Sponsors";
import { Chat } from "./screens/Chat";
import { Dashboard } from "./screens/Dashboard";
import { Developer } from "./screens/Developer";
import { Review } from "./screens/Review";

export type Route = "dashboard" | "forecast" | "review" | "how" | "chat" | "developer";

const PATHS: Record<string, Route> = { "": "dashboard", forecast: "forecast", review: "review", "how-it-works": "how", chat: "chat", developer: "developer" };

function useRoute(): Route {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return PATHS[hash.replace(/^#\/?/, "").split("?")[0]] ?? "dashboard";
}

export default function App() {
  const route = useRoute();
  const [restaurant, setRestaurant] = useState<string | null>(() => recall("boh-restaurant"));
  const [fresh, setFresh] = useState(0); // bumped to start over with a new check
  const main = useRef<HTMLElement>(null);
  const first = useRef(true);

  // A new page starts at the top, with focus on its content so screen readers start there.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    window.scrollTo(0, 0);
    main.current?.focus({ preventScroll: true });
  }, [route]);

  const connected = (name: string | null) => {
    setRestaurant(name);
    remember("boh-restaurant", name);
  };

  // A new check starts over in the Chat (where Get started is), with a fresh conversation.
  const newCheck = () => {
    remember("boh-run", null);
    remember("boh-chat", null);
    connected(null);
    setFresh((n) => n + 1);
    window.location.hash = "#/chat";
    window.scrollTo(0, 0);
  };

  return (
    <>
      <a
        className="skip-link"
        href="#main"
        onClick={(e) => {
          e.preventDefault(); // the hash is the page address here, so jump by focus instead
          main.current?.focus();
        }}
      >
        Skip to content
      </a>
      <Header route={route} restaurant={restaurant} onNewCheck={newCheck} />
      <main id="main" ref={main} tabIndex={-1} className="outline-none">
        {route === "chat" ? (
          <Chat key={fresh} restaurant={restaurant} onRestaurant={connected} />
        ) : route === "developer" ? (
          <Developer />
        ) : route === "review" ? (
          <Review />
        ) : (
          <Dashboard key={fresh} view={route} onRestaurant={connected} />
        )}
        {route === "dashboard" && <Sponsors />}
      </main>
      {route === "dashboard" && <Footer />}
    </>
  );
}
