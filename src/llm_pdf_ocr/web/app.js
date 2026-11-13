// The page for LLM PDF OCR (llm-pdf-ocr-api). No build step and no other origin: it talks to this
// service only. Text from the PDF is set with textContent, never as HTML.
"use strict";

const POLL_MS = 1000;
const SAMPLE = "/static/samples/sample.pdf";

const $ = (id) => document.getElementById(id);
const form = $("extract");
const drop = $("drop");
const fileInput = $("file");
const dropText = $("drop-text");
const emptyState = $("empty-state");
const sample = $("sample");
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

// Work in progress: the words, then three bouncing dots, as llm-pdf-chatbot shows
// "Thinking". A screen reader hears the words and an ellipsis.
function showBusy(text) {
  const busy = el("span", "thinking");
  const dots = el("span", "thinking-dots");
  dots.setAttribute("aria-hidden", "true");
  dots.append(el("span", "thinking-dot"), el("span", "thinking-dot"), el("span", "thinking-dot"));
  busy.append(el("span", "thinking-label", text), el("span", "visually-hidden", "…"), dots);
  statusLine.classList.remove("error");
  statusLine.replaceChildren(busy);
}

function showFile() {
  const file = fileInput.files[0];
  dropText.textContent = file ? file.name : "Drop a PDF here, or click to pick one";
  emptyState.hidden = Boolean(file);
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
  $("vision-help").hidden = false;
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
    showBusy(readingLine(job.pages_done, job.pages_total));
    showProgress(job.pages_done, job.pages_total);
    await sleep(POLL_MS);
  }
}

// "Reading page 2 of 3" while page 2 is read; just "Reading" until the job knows its pages.
function readingLine(done, total) {
  if (!total) return "Reading";
  return `Reading page ${Math.min(done + 1, total)} of ${total}`;
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

// The Method select's names, for the page cards. The Markdown and JSON keep the API's ids.
const METHOD_NAMES = { text_layer: "Text layer", tesseract: "Tesseract", vision: "Vision model" };

function methodLabel(page) {
  let line = `Method: ${METHOD_NAMES[page.method] || page.method}`;
  if (page.model !== null) line += ` · Model: ${page.model}`;
  if (page.confidence !== null) line += ` · Confidence: ${page.confidence.toFixed(2)}`;
  return line;
}

// One card per page: its image beside its text, its method, and why it failed if it did.
// The image is a thumbnail that opens full size in a new tab.
function pageCard(jobId, page) {
  const card = el("li", page.status === "failed" ? "page-result failed" : "page-result");
  const figure = el("div", "page-figure");
  const image = el("img", "page-image");
  image.alt = `Page ${page.page}`;
  image.loading = "lazy";
  const url = `/v1/jobs/${encodeURIComponent(jobId)}/pages/${page.page}.png`;
  const link = el("a", "page-link");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  link.title = `Open page ${page.page} full size`;
  image.addEventListener("error", () => link.replaceWith(el("p", "no-image", "Image not available")));
  image.src = url;
  link.append(image);
  figure.append(link);

  const body = el("div", "page-body");
  body.append(el("h3", "", `Page ${page.page}`), el("p", "method", methodLabel(page)));
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
  findInput.value = "";
  clearFind();
}

// Find in the result: a plain-text, case-insensitive search over each page's text and the
// Markdown or JSON output. It never leaves the browser. Typing is debounced; one pass over
// each text collects the matches, and each text's DOM is rebuilt at most once per pass.
const FIND_DEBOUNCE_MS = 150;
const findInput = $("find-input");
const findCount = $("find-count");
const findPrev = $("find-prev");
const findNext = $("find-next");

let findMarks = []; // every highlight, in document order
let findCurrent = -1; // index of the current match, -1 when there is none
let findCurrentMark = null;
let findTimer = null;
let findQuery = ""; // the query the highlights were built from

// The texts find searches, in document order: each page's text, then the whole output.
function findTexts() {
  return [...pageList.querySelectorAll(".text"), result];
}

function clearFind() {
  clearTimeout(findTimer);
  findTimer = null;
  findQuery = "";
  findMarks = [];
  findCurrent = -1;
  findCurrentMark = null;
  for (const node of findTexts()) {
    if (node.querySelector("mark")) node.textContent = node.textContent;
  }
  findCount.textContent = "";
  findPrev.disabled = true;
  findNext.disabled = true;
}

// Lowercase one character at a time, so each unit of the lowercased text knows which
// character of the original it came from: lowercasing can change the length ("İ" becomes
// "i" and a combining dot). Final sigma is folded to sigma, as whole-word lowercasing
// would have chosen either.
function lowerChars(text) {
  let lower = "";
  const from = []; // from[k]: where the character behind lower[k] starts in text
  const to = []; // to[k]: where it ends
  for (let i = 0; i < text.length; ) {
    const ch = String.fromCodePoint(text.codePointAt(i));
    const low = ch.toLowerCase().replace("ς", "σ");
    for (let k = 0; k < low.length; k++) {
      from.push(i);
      to.push(i + ch.length);
    }
    lower += low;
    i += ch.length;
  }
  return { lower, from, to };
}

// One pass over the text: every [start, end) of the query in the original text,
// case-insensitive and literal (no regex). A match that covers part of an expanded
// character takes the whole character. Matches do not overlap.
function matchRanges(text, query) {
  const hay = lowerChars(text);
  const needle = lowerChars(query).lower;
  const ranges = [];
  for (let i = hay.lower.indexOf(needle); i !== -1; ) {
    const start = hay.from[i];
    const end = hay.to[i + needle.length - 1];
    ranges.push([start, end]);
    let next = i + needle.length;
    while (next < hay.from.length && hay.from[next] < end) next++;
    i = hay.lower.indexOf(needle, next);
  }
  return ranges;
}

// Rebuild one text once: text nodes and <mark> elements, never HTML.
function highlightText(node, query) {
  const text = node.textContent;
  const ranges = matchRanges(text, query);
  if (!ranges.length) {
    if (node.querySelector("mark")) node.textContent = text;
    return;
  }
  const frag = document.createDocumentFragment();
  let pos = 0;
  for (const [start, end] of ranges) {
    if (start > pos) frag.append(text.slice(pos, start));
    frag.append(el("mark", "find-match", text.slice(start, end)));
    pos = end;
  }
  if (pos < text.length) frag.append(text.slice(pos));
  node.replaceChildren(frag);
}

function setCurrent(index) {
  if (findCurrentMark) findCurrentMark.classList.remove("current");
  findCurrent = index;
  findCurrentMark = findMarks[index] || null;
  if (findCurrentMark) findCurrentMark.classList.add("current");
  findCount.textContent = findMarks.length
    ? `${index + 1} of ${findMarks.length}`
    : findInput.value
      ? "No matches"
      : "";
  findPrev.disabled = !findMarks.length;
  findNext.disabled = !findMarks.length;
}

function runFind() {
  findTimer = null;
  const query = findInput.value;
  if (query === findQuery) return;
  findQuery = query;
  if (!query) {
    clearFind();
    return;
  }
  for (const node of findTexts()) highlightText(node, query);
  findMarks = [...results.querySelectorAll("mark.find-match")];
  findCurrentMark = null; // the old marks are gone with the rebuilt texts
  setCurrent(findMarks.length ? 0 : -1);
}

// Move to the next or previous match, wrapping, and bring it into view.
function stepFind(delta) {
  if (findTimer) {
    clearTimeout(findTimer); // a step right after typing searches the new query first
    runFind();
  }
  if (!findMarks.length) return;
  const n = findMarks.length;
  setCurrent((((findCurrent + delta) % n) + n) % n);
  // Open the page if it is collapsed, then scroll the match into view.
  for (let node = findCurrentMark.parentElement; node; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.open = true;
  }
  findCurrentMark.scrollIntoView({ block: "center", inline: "nearest" });
}

findInput.addEventListener("input", () => {
  clearTimeout(findTimer);
  findTimer = setTimeout(runFind, FIND_DEBOUNCE_MS);
});
findInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    stepFind(event.shiftKey ? -1 : 1);
  } else if (event.key === "Escape") {
    findInput.value = "";
    clearFind();
  }
});
findPrev.addEventListener("click", () => stepFind(-1));
findNext.addEventListener("click", () => stepFind(1));

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
  sample.disabled = true;
  clearResult();
  showBusy("Uploading");
  try {
    const response = await request("/v1/jobs", { method: "POST", body: data });
    if (!response.ok) throw new Error(await errorMessage(response));
    const id = (await response.json()).id;
    const job = await follow(id);
    const doc = job.result;
    if (!doc || !Array.isArray(doc.pages)) throw new Error("the job returned no result");
    showResult(id, doc, format);
    const failed = doc.failed_pages.length ? `, ${doc.failed_pages.length} failed` : "";
    const pages = `${doc.page_count} ${doc.page_count === 1 ? "page" : "pages"}`;
    showStatus(`Done: ${pages}${failed}.`, doc.failed_pages.length > 0);
  } catch (err) {
    clearResult();
    showStatus(err.message, true);
  } finally {
    progress.hidden = true;
    submit.disabled = false;
    sample.disabled = false;
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

// Load the bundled sample PDF into the file field and read it, so a first visitor sees a
// result without a file of their own.
async function trySample() {
  sample.disabled = true;
  try {
    const response = await request(SAMPLE);
    if (!response.ok) throw new Error(`Could not load the sample (${response.status}).`);
    const file = new File([await response.blob()], "sample.pdf", { type: "application/pdf" });
    const files = new DataTransfer();
    files.items.add(file);
    fileInput.files = files.files;
    showFile();
    sample.disabled = false; // extract() disables it again while the job runs
    form.requestSubmit(submit);
  } catch (err) {
    sample.disabled = false;
    showStatus(err.message, true);
  }
}

sample.addEventListener("click", trySample);
method.addEventListener("change", syncModelField);
form.addEventListener("submit", extract);

loadCapabilities().catch(() => {});
