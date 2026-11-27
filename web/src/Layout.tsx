import { Github, Moon, ScanLine, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, ScrollRestoration } from "react-router-dom";
import { api } from "./api";
import { Spinner } from "./components";

type Theme = "light" | "dark";

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage unavailable */
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Free hosting sleeps when idle. Ping /health on load and explain the wait if it is slow. */
function useWakeBanner() {
  const [state, setState] = useState<"idle" | "slow" | "down">("idle");
  useEffect(() => {
    let done = false;
    const slow = setTimeout(() => !done && setState("slow"), 1500);
    const attempt = (tries: number) =>
      api
        .health()
        .then(() => {
          done = true;
          setState("idle");
        })
        .catch(() => {
          if (tries > 0) setTimeout(() => attempt(tries - 1), 4000);
          else {
            done = true;
            setState("down");
          }
        });
    attempt(20);
    return () => clearTimeout(slow);
  }, []);
  return state;
}

export default function Layout() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const wake = useWakeBanner();

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* storage unavailable */
    }
  }, [theme]);

  const link = ({ isActive }: { isActive: boolean }) => `nav-link${isActive ? " active" : ""}`;

  return (
    <>
      <header className="nav">
        <div className="container nav-inner">
          <NavLink to="/" className="brand" aria-label="Recon Agent home">
            <span className="brand-mark">
              <ScanLine size={17} />
            </span>
            <span className="brand-text">Recon Agent</span>
          </NavLink>
          <nav className="nav-links">
            <NavLink to="/" end className={link}>
              Overview
            </NavLink>
            <NavLink to="/run" className={link}>
              Run
            </NavLink>
            <NavLink to="/guard" className={link}>
              Guard Lab
            </NavLink>
            <NavLink to="/runs" end className={link}>
              History
            </NavLink>
          </nav>
          <span className="spacer" />
          <a
            className="icon-btn hide-sm"
            href="https://github.com/Prathamcodin/settlement-recon-agent"
            target="_blank"
            rel="noreferrer"
            aria-label="Source on GitHub"
          >
            <Github size={17} />
          </a>
          <button
            className="icon-btn"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
        </div>
      </header>

      <main>
        <Outlet />
      </main>
      <ScrollRestoration />

      <footer className="footer">
        <div className="container row wrap gap-12">
          <span>Settlement Reconciliation Agent · built by Pratham Goyal</span>
          <span className="spacer" />
          <span>FastAPI · PostgreSQL · React · TypeScript</span>
        </div>
      </footer>

      {wake === "slow" && (
        <div className="banner" role="status">
          <Spinner /> Waking the server. Free hosting sleeps when idle; this takes up to a minute.
        </div>
      )}
      {wake === "down" && (
        <div className="banner" role="alert">
          The API is not answering right now. Please try again later.
        </div>
      )}
    </>
  );
}
