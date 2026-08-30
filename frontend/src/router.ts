import { useEffect, useState } from "react";

export type AppRoute =
  | { kind: "home" }
  | { kind: "analysis"; analysisId: string }
  | { kind: "demo"; caseId: string }
  | { kind: "not-found" };

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CASE_PATTERN = /^[a-z0-9][a-z0-9-]{0,99}$/;

export function parseHash(hash: string): AppRoute {
  const path = hash.replace(/^#\/?/, "").replace(/\/$/, "");
  if (path === "") return { kind: "home" };
  const [section, identity, ...rest] = path.split("/");
  if (rest.length > 0 || identity === undefined) return { kind: "not-found" };
  let decoded: string;
  try {
    decoded = decodeURIComponent(identity);
  } catch {
    return { kind: "not-found" };
  }
  if (section === "analysis" && UUID_PATTERN.test(decoded)) {
    return { kind: "analysis", analysisId: decoded };
  }
  if (section === "demo" && CASE_PATTERN.test(decoded)) {
    return { kind: "demo", caseId: decoded };
  }
  return { kind: "not-found" };
}

export function analysisHref(analysisId: string): string {
  return `#analysis/${encodeURIComponent(analysisId)}`;
}

export function demoHref(caseId: string): string {
  return `#demo/${encodeURIComponent(caseId)}`;
}

export function useHashRoute(): AppRoute {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const update = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}
