// The page for llm-pdf-ocr-api. No build step and no other origin: it talks to this
// service only. Text from the PDF is set with textContent, never as HTML.
"use strict";

const POLL_MS = 1000;

const $ = (id) => document.getElementById(id);
const form = $("extract");
const drop = $("drop");
const fileInput = $("file");
const dropText = $("drop-text");
const empty = $("empty");
const method = $("method");
const modelField = $("model-field");
const model = $("model");
const submit = $("submit");
const statusLine = $("status");
const progress = $("progress");
const results = $("results");
const pageList = $("pages");
const result = $("result");

function showStatus(text, isError = false) {
  statusLine.textContent = text;
  statusLine.classList.toggle("error", isError);
}

function showFile() {
  const file = fileInput.files[0];
  dropText.textContent = file ? file.name : "Drop a PDF here, or click to pick one";
  empty.hidden = Boolean(file);
}

// The API's error message, with its request id so a user can quote it to the operator.
async function errorMessage(response) {
  try {
    const { error } = await response.json();
    if (error && error.message) {
      return error.request_id ? `${error.message} (Request ID: ${error.request_id})` : error.message;
    }
  } catch (_) {
    // not JSON: fall through to the status
  }
  return `request failed (${response.status})`;
}

// fetch, with a plain message in place of the browser's own ("Failed to fetch") when
// the service cannot be reached at all.
async function request(url, options) {
  try {
    return await fetch(url, options);
  } catch (_) {
    throw new Error("Could not reach the service. Check the connection and try again.");
  }
}

// Offer the vision method, and its models, only when the operator configured one.
async function loadCapabilities() {
  const response = await fetch("/v1/capabilities");
  if (!response.ok) return;
  const caps = await response.json();
  if (!caps.methods.includes("vision")) return;
  const vision = method.querySelector('option[value="vision"]');
  vision.disabled = false;
  vision.hidden = false;
  for (const name of caps.models) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    model.append(option);
  }
}

function syncModelField() {
  modelField.hidden = method.value !== "vision";
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function follow(id) {
  for (;;) {
    const response = await request(`/v1/jobs/${encodeURIComponent(id)}`);
    if (!response.ok) throw new Error(await errorMessage(response));
    const job = await response.json();
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error((job.error && job.error.message) || "the job failed");
    showStatus(`Reading pages: ${job.pages_done} of ${job.pages_total || "?"}`);
    showProgress(job.pages_done, job.pages_total);
    await sleep(POLL_MS);
  }
}

function showProgress(done, total) {
  progress.hidden = false;
  if (total) {
    progress.max = total;
    progress.value = done;
  } else {
    progress.removeAttribute("value"); // indeterminate until the job knows its pages
  }
}

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

function methodLine(page) {
  let line = `Method: ${page.method}`;
  if (page.model !== null) line += ` · Model: ${page.model}`;
  if (page.confidence !== null) line += ` · Confidence: ${page.confidence.toFixed(2)}`;
  return line;
}

function fallbackLine(fallback) {
  const { method, model, error } = fallback;
  return `Read with Tesseract because ${method} (${model}) failed (${error.code}): ${error.message}`;
}

// One card per page: its image beside its text, its method, and why it failed if it did.
function pageCard(jobId, page) {
  const card = el("li", page.status === "failed" ? "page-result failed" : "page-result");
  const figure = el("div", "page-figure");
  const image = el("img", "page-image");
  image.alt = `Page ${page.page}`;
  image.loading = "lazy";
  image.addEventListener("error", () => image.replaceWith(el("p", "no-image", "Image not available")));
  image.src = `/v1/jobs/${encodeURIComponent(jobId)}/pages/${page.page}.png`;
  figure.append(image);

  const body = el("div", "page-body");
  body.append(el("h3", "", `Page ${page.page}`), el("p", "method", methodLine(page)));
  if (page.fallback) body.append(el("p", "fallback", fallbackLine(page.fallback)));
  if (page.error) {
    body.append(el("p", "failure", `Failed (${page.error.code}): ${page.error.message}`));
  } else {
    body.append(el("pre", "text", page.text));
  }
  card.append(figure, body);
  return card;
}

// The Markdown /v1/extract returns (render_markdown in extract.py), built from the JSON.
function markdown(doc) {
  const sections = doc.pages.map((page) => {
    const lines = [`## Page ${page.page}`, "", methodLine(page)];
    if (page.fallback) lines.push("", `> ${fallbackLine(page.fallback)}`);
    if (page.error) {
      lines.push("", `> **Page ${page.page} failed** (${page.error.code}): ${page.error.message}`);
    } else if (page.text) {
      lines.push("", page.text);
    }
    return lines.join("\n");
  });
  return sections.join("\n\n") + "\n";
}

let current = null; // the finished document, for copy and download

function download(name, type, text) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = el("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function copyMarkdown() {
  if (!current) return;
  try {
    await navigator.clipboard.writeText(markdown(current));
    showStatus("Markdown copied.");
  } catch (_) {
    showStatus("Could not copy to the clipboard.", true);
  }
}

$("copy").addEventListener("click", copyMarkdown);
$("download-md").addEventListener("click", () => {
  if (current) download("result.md", "text/markdown", markdown(current));
});
$("download-json").addEventListener("click", () => {
  if (current) download("result.json", "application/json", JSON.stringify(current, null, 2));
});

function showResult(jobId, doc, format) {
  current = doc;
  pageList.replaceChildren(...doc.pages.map((page) => pageCard(jobId, page)));
  result.textContent = format === "json" ? JSON.stringify(doc, null, 2) : markdown(doc);
  results.hidden = false;
}

function clearResult() {
  current = null;
  results.hidden = true;
  pageList.replaceChildren();
  result.textContent = "";
}

async function extract(event) {
  event.preventDefault();
  if (!fileInput.files[0]) {
    showStatus("Pick a PDF first.", true);
    return;
  }
  // The job is always asked for JSON: only it carries each page's method and error.
  // The Markdown output is built from it.
  const format = $("format").value;
  const data = new FormData();
  data.append("file", fileInput.files[0]);
  data.append("format", "json");
  data.append("method", method.value);
  if (method.value === "vision" && model.value) data.append("model", model.value);

  submit.disabled = true;
  clearResult();
  showStatus("Uploading…");
  try {
    const response = await request("/v1/jobs", { method: "POST", body: data });
    if (!response.ok) throw new Error(await errorMessage(response));
    const id = (await response.json()).id;
    const job = await follow(id);
    const doc = job.result;
    if (!doc || !Array.isArray(doc.pages)) throw new Error("the job returned no result");
    showResult(id, doc, format);
    const failed = doc.failed_pages.length ? `, ${doc.failed_pages.length} failed` : "";
    showStatus(`Done: ${doc.page_count} page(s)${failed}.`, doc.failed_pages.length > 0);
  } catch (err) {
    clearResult();
    showStatus(err.message, true);
  } finally {
    progress.hidden = true;
    submit.disabled = false;
  }
}

for (const name of ["dragenter", "dragover"]) {
  drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.add("over");
  });
}
for (const name of ["dragleave", "drop"]) {
  drop.addEventListener(name, () => drop.classList.remove("over"));
}
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer.files.length) {
    fileInput.files = event.dataTransfer.files;
    showFile();
  }
});
fileInput.addEventListener("change", showFile);
method.addEventListener("change", syncModelField);
form.addEventListener("submit", extract);

loadCapabilities().catch(() => {});
