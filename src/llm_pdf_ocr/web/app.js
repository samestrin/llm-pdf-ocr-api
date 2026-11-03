// The page for llm-pdf-ocr-api. No build step and no other origin: it talks to this
// service only. Text from the PDF is set with textContent, never as HTML.
"use strict";

const POLL_MS = 1000;

const $ = (id) => document.getElementById(id);
const form = $("extract");
const drop = $("drop");
const fileInput = $("file");
const dropText = $("drop-text");
const method = $("method");
const modelField = $("model-field");
const model = $("model");
const submit = $("submit");
const statusLine = $("status");
const result = $("result");

function showStatus(text, isError = false) {
  statusLine.textContent = text;
  statusLine.classList.toggle("error", isError);
}

function showFile() {
  const file = fileInput.files[0];
  dropText.textContent = file ? file.name : "Drop a PDF here, or click to pick one";
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (body.error && body.error.message) return body.error.message;
  } catch (_) {
    // not JSON: fall through to the status
  }
  return `request failed (${response.status})`;
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
    const response = await fetch(`/v1/jobs/${encodeURIComponent(id)}`);
    if (!response.ok) throw new Error(await errorMessage(response));
    const job = await response.json();
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error(job.error || "the job failed");
    showStatus(`Reading pages: ${job.pages_done} of ${job.pages_total || "?"}`);
    await sleep(POLL_MS);
  }
}

async function extract(event) {
  event.preventDefault();
  if (!fileInput.files[0]) {
    showStatus("Pick a PDF first.", true);
    return;
  }
  const data = new FormData();
  data.append("file", fileInput.files[0]);
  data.append("format", $("format").value);
  data.append("method", method.value);
  if (method.value === "vision" && model.value) data.append("model", model.value);

  submit.disabled = true;
  result.hidden = true;
  result.textContent = "";
  showStatus("Uploading…");
  try {
    const response = await fetch("/v1/jobs", { method: "POST", body: data });
    if (!response.ok) throw new Error(await errorMessage(response));
    const job = await follow((await response.json()).id);
    const text = typeof job.result === "string" ? job.result : JSON.stringify(job.result, null, 2);
    result.textContent = text;
    result.hidden = false;
    showStatus(`Done: ${job.pages_total} page(s).`);
  } catch (err) {
    showStatus(err.message, true);
  } finally {
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
