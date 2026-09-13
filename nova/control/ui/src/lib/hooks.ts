import * as React from "react";
import { load, type Loaded } from "@/lib/api";

/** One panel's data, refreshed on an interval, each panel independent of the others. */
export function usePanel<T>(path: string, refreshMs = 15000): Loaded<T> {
  const [state, setState] = React.useState<Loaded<T>>({ state: "loading" });

  React.useEffect(() => {
    let alive = true;
    const tick = async () => {
      const next = await load<T>(path);
      if (alive) setState(next);
    };
    void tick();
    const id = window.setInterval(tick, refreshMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [path, refreshMs]);

  return state;
}

/** Counts up to a target. Purely decorative, so it degrades to the final value at once
 *  when the operator has asked for reduced motion. */
export function useCountUp(target: number, durationMs = 700) {
  const [value, setValue] = React.useState(0);

  React.useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced || !Number.isFinite(target)) {
      setValue(target);
      return;
    }
    let frame = 0;
    const started = performance.now();
    const step = (now: number) => {
      const progress = Math.min(1, (now - started) / durationMs);
      // easeOutCubic: fast enough to feel responsive, settled enough to read.
      setValue(Math.round(target * (1 - Math.pow(1 - progress, 3))));
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, durationMs]);

  return value;
}

/** Theme, remembered. Defaults to dark: this is a wall-display page as often as a tab. */
export function useTheme() {
  const [theme, setTheme] = React.useState<"dark" | "light">(() => {
    try {
      return (localStorage.getItem("nova-theme") as "dark" | "light") ?? "dark";
    } catch {
      return "dark";
    }
  });

  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try {
      localStorage.setItem("nova-theme", theme);
    } catch {
      /* a browser with storage blocked still gets a working toggle, just not a sticky one */
    }
  }, [theme]);

  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}
