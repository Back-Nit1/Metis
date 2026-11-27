import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { HudScreen } from "./screens/HudScreen";
import { initTheme } from "./lib/theme";
import "./styles.css";

initTheme();

const root = document.getElementById("root");
if (!root) throw new Error("the #root element is missing from index.html");

createRoot(root).render(
  <StrictMode>
    {window.location.hash.startsWith("#/hud") ? <HudScreen /> : <App />}
  </StrictMode>,
);

