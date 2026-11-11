// The theme toggle, as in llm-pdf-chatbot: the page follows the system light or dark
// scheme until the toggle saves a choice. Loaded in <head> without `defer`, so a saved
// choice is applied before the page is drawn. The button's label names what a click does.
"use strict";

(() => {
  const KEY = "theme";
  const root = document.documentElement;

  const saved = () => {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (_) {
      return null; // storage can be blocked: follow the system
    }
  };
  const system = () => (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const current = () => root.dataset.theme || system();

  const choice = saved();
  if (choice) root.dataset.theme = choice;

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const label = () => {
      button.textContent = current() === "dark" ? "Light theme" : "Dark theme";
    };
    label();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", label);
    button.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem(KEY, next);
      } catch (_) {
        // the choice then lasts until the page closes
      }
      label();
    });
  });
})();
