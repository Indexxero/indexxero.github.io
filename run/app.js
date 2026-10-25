/**
 * The page's controller: collect two files and a mapping, hand them to the
 * worker, turn what comes back into a zip and a rendered report.
 *
 * THE MAPPING IS A FORM, NOT A JSON BOX. A partner asked to hand-write json
 * will mistype a brace and read the parse error as "this tool is broken". The
 * form starts from mapping.example.json and writes the json itself.
 */
import { makeZip, sha256 } from './zip.js';

const $ = (id) => document.getElementById(id);
// pilot.py writes thirteen; the page adds receipt.json and mapping-used.json.
const KIT_FILES = 13;
const ZIP_FILES = KIT_FILES + 2;

/** The kit version as the run itself recorded it, not re-derived from source. */
const kitVersionFromReport = (files) =>
  JSON.parse(new TextDecoder().decode(files['manifest.json'])).kit_version ?? 'unknown';

let template = null;      // mapping.example.json, as loaded
let worker = null;
let picked = { schedule: null, invoices: null };
let pilotSha = null;          // from the worker, hashed there, fetched once
let ticker = null;            // the elapsed-seconds ticker, see startTicker

/**
 * Elapsed seconds, and why it is seconds rather than stages.
 *
 * `pilot.py` prints nothing until it is finished: its only `print` calls are
 * the closing "done:" line and its warnings. So the page CANNOT show real
 * stages without editing it, and editing it is out of scope. A fake stage bar
 * that advanced on a timer would be worse than a number, because it would be
 * claiming progress nobody measured.
 *
 * What this shows is true: how long it has been running and how much it was
 * given. Measured need for it: the 3,000 customer shape runs 46 seconds, and a
 * Playwright run sampling the status line 65 times read the same static string
 * every time. The tab was alive the whole while and looked hung.
 */
function startTicker(sizeNote) {
  const t0 = Date.now();
  clearInterval(ticker);
  ticker = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    $('statusDetail').textContent = `${sizeNote}, running ${s}s. pilot.py reports when it finishes, so this is elapsed time, not a stage.`;
  }, 1000);
}
const stopTicker = () => { clearInterval(ticker); ticker = null; };

const setStatus = (text, detail = '') => {
  $('status').textContent = text;
  $('statusDetail').textContent = detail;
};

// ---- the mapping form ------------------------------------------------------
// Only the column-name fields are editable here. The rules block (tolerance,
// which line types count as recurring) is shown read-only: changing it changes
// what "supported" means, and that belongs in a conversation, not a text box
// on a first run.
const FIELD_HELP = {
  customer_id: 'The id used in both exports, if there is one.',
  customer_name: 'Used when there is no id, or to check the id.',
  snapshot_date: 'The month this row states. Snapshot format only.',
  mrr: 'Monthly recurring revenue.',
  arr: 'Annual. Leave empty if the export has MRR.',
  start_date: 'Contract start. Contract format needs it.',
  end_date: 'Contract end, if present.',
  status: 'Active, churned, and so on.',
  invoice_id: 'Repeated ids are a finding, so this matters.',
  invoice_date: 'When the invoice was raised.',
  service_start: 'The period the line covers, if present.',
  service_end: 'The other end of that period.',
  amount: 'The line amount. Symbols and commas are fine.',
  line_type: 'Recurring, credit, services, and so on.',
  description: 'Free text, used only in the findings.',
};

function buildMappingForm(map) {
  for (const section of ['schedule', 'invoices']) {
    const host = $(`map-${section}`);
    host.innerHTML = '';
    for (const [key, value] of Object.entries(map[section])) {
      if (key.startsWith('_')) continue;
      const row = document.createElement('label');
      row.className = 'maprow';
      if (key === 'format') {
        row.innerHTML = `<span class="k">schedule shape</span>
          <select id="f-${section}-${key}">
            ${['snapshot', 'contract', 'wide'].map(o => `<option ${o === value ? 'selected' : ''}>${o}</option>`).join('')}
          </select>
          <span class="h">snapshot: a row per customer per month. contract: a row per customer with dates. wide: a column per month.</span>`;
      } else {
        row.innerHTML = `<span class="k">${key}</span>
          <input id="f-${section}-${key}" value="${value ?? ''}" placeholder="(no such column)" spellcheck="false">
          <span class="h">${FIELD_HELP[key] ?? ''}</span>`;
      }
      host.appendChild(row);
    }
  }
}

/**
 * The rules in force, shown rather than assumed.
 *
 * SP-WEB: the form edits column names only, and `mappingFromForm` deep copies
 * `template`. So before a mapping file could be loaded, the page ran every
 * client's data under `mapping.example.json`'s rules: its `recurring_types`,
 * its `credit_types`, its tolerance. Measured on the stress set, three of five
 * shapes came back with `report.md`, `accounts.csv` and
 * `compare_customer_month.csv` DIFFERENT from the native run, because those
 * shapes name line types the example does not list (`invoice`, `credit_note`,
 * `metered`). Two shapes matched, which is the dangerous part: it looks correct
 * on the example and on any export whose types happen to be the defaults.
 */
function renderRules() {
  const r = (template && template.rules) || {};
  const shown = Object.fromEntries(Object.entries(r).filter(([k]) => !k.startsWith('_')));
  $('rulesView').textContent = JSON.stringify(shown, null, 2);
}

function mappingFromForm() {
  const out = JSON.parse(JSON.stringify(template));
  for (const section of ['schedule', 'invoices']) {
    for (const key of Object.keys(out[section])) {
      if (key.startsWith('_')) continue;
      const el = $(`f-${section}-${key}`);
      if (el) out[section][key] = el.value.trim();
    }
  }
  return JSON.stringify(out, null, 2);
}

// ---- running ---------------------------------------------------------------
function ensureWorker() {
  if (worker) return worker;
  worker = new Worker('./worker.js', { type: 'module' });
  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === 'progress') setStatus(m.stage, m.detail);
    else if (m.type === 'warm-done') {
      pilotSha = m.pilotSha;
      setStatus('Ready', 'Python and pandas are loaded. From here on this page fetches nothing.');
    }
    else if (m.type === 'error') {
      stopTicker();
      setStatus('It stopped, and here is what it said', '');
      $('error').hidden = false;
      $('error').textContent = m.message;
      $('run').disabled = false;
    } else if (m.type === 'done') finish(m);
  };
  return worker;
}

async function finish({ files, printed, elapsedMs }) {
  stopTicker();
  const mappingText = mappingFromForm();

  // THE MAPPING THE RUN ACTUALLY USED GOES IN THE ZIP, and it is a fifteenth
  // file rather than the thirteen plus the receipt.
  //
  // WHY, MEASURED. `receipt.py verify` on the page's own output returned
  // `MISMATCH input mapping` with every other line `ok`. The page builds the
  // mapping from the form, so its bytes are not the bytes of any file on disk:
  // the receipt hashed something nobody else held. A receipt whose inputs
  // cannot be recomputed proves nothing, which is the one thing it exists to
  // do. With the file in the zip, `verify --map out/mapping-used.json` can
  // recompute it, and the same zip verifies on any laptop.
  //
  // It is a deviation from "the same thirteen files" and Cowork should rule on
  // it. The alternative is a receipt that only verifies for an unedited
  // example, which is to say for no real engagement.
  files['mapping-used.json'] = new TextEncoder().encode(mappingText);

  const names = Object.keys(files).sort();
  // The receipt, same keys and order as arr-check/receipt.py (CP-11), written
  // here because pilot.py builds its own by calling receipt.py as a subprocess
  // and subprocess.run raises OSError under Pyodide.
  const kitVersion = kitVersionFromReport(files);
  const manifest = JSON.parse(new TextDecoder().decode(files['manifest.json']));
  const rules = JSON.parse(mappingText).rules || {};

  // THE SHAPE IS receipt.py's, NOT THIS FILE'S READING OF THE SPEC.
  // CP-11's wording ("inputs: file name, bytes, sha256 for schedule, invoices,
  // mapping") reads as a list and the implementation is a dict keyed by role,
  // with the key `file` rather than `name`. Writing the list made
  // `receipt.py verify` die with `TypeError: list indices must be integers`.
  // The authority is the verifier, so this mirrors `build()` field for field:
  // inputs keyed schedule/invoices/mapping, outputs a list sorted by name and
  // excluding receipt.json, parameters carrying `asof_month` plus the four rule
  // values, made_utc as "%Y-%m-%d %H:%M:%S" in UTC.
  const mappingBytes = new TextEncoder().encode(mappingText);
  const entry = async (name, bytes) => ({ bytes: bytes.length, file: name, sha256: await sha256(bytes) });

  const inputs = {
    invoices: await entry(picked.invoices.name, picked.invoicesBytes),
    mapping: await entry('mapping.json', mappingBytes),
    schedule: await entry(picked.schedule.name, picked.scheduleBytes),
  };
  const outputs = [];
  for (const n of names) outputs.push(await entry(n, files[n]));

  const receipt = {
    inputs,
    kit_version: kitVersion,
    made_utc: new Date().toISOString().slice(0, 19).replace('T', ' '),
    outputs,
    parameters: {
      asof_month: manifest.asof_month ?? null,
      months_for_retention: rules.months_for_retention ?? null,
      ramp_months: rules.ramp_months ?? null,
      tolerance_abs: rules.tolerance_abs ?? null,
      tolerance_pct: rules.tolerance_pct ?? null,
    },
    pilot_sha256: pilotSha,
  };
  const sortDeep = (v) => Array.isArray(v) ? v.map(sortDeep)
    : (v && typeof v === 'object') ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sortDeep(v[k])])) : v;
  files['receipt.json'] = new TextEncoder().encode(JSON.stringify(sortDeep(receipt), null, 2) + '\n');

  const blob = makeZip(files);
  const a = $('download');
  a.href = URL.createObjectURL(blob);
  a.download = `arr-check-${$('asof').value || 'run'}.zip`;
  a.hidden = false;
  $('reportWrap').hidden = false;
  $('report').textContent = new TextDecoder().decode(files['report.md']);
  $('printed').textContent = printed.trim();
  // Counted AFTER receipt.json is added. The first version counted before and
  // reported "expected 14 files, got 13" on a zip that did contain 14: the
  // message was wrong, not the zip, which is its own small lesson about
  // trusting a count taken at the wrong moment.
  const zipped = Object.keys(files).length;
  const short = zipped === ZIP_FILES ? '' : ` (expected ${ZIP_FILES}, got ${zipped})`;
  setStatus(`Done in ${(elapsedMs / 1000).toFixed(1)}s`, `${zipped} files in the zip${short}. Nothing was sent anywhere.`);
  $('run').disabled = false;
}

async function run() {
  if (!picked.schedule || !picked.invoices) { setStatus('Two files are needed first', ''); return; }
  $('error').hidden = true; $('download').hidden = true; $('reportWrap').hidden = true;
  $('run').disabled = true;
  setStatus('Reading the files', 'in this tab');
  picked.scheduleBytes = new Uint8Array(await picked.schedule.arrayBuffer());
  picked.invoicesBytes = new Uint8Array(await picked.invoices.arrayBuffer());
  const mb = (n) => (n / 1048576).toFixed(1);
  startTicker(`${mb(picked.scheduleBytes.length + picked.invoicesBytes.length)} MB of exports`);
  ensureWorker().postMessage({
    schedule: picked.scheduleBytes.slice().buffer, invoices: picked.invoicesBytes.slice().buffer,
    scheduleName: picked.schedule.name, invoicesName: picked.invoices.name,
    mapping: mappingFromForm(), asof: $('asof').value,
  });
}

// ---- the example -----------------------------------------------------------
async function loadExample() {
  setStatus('Loading the Larkspur example', 'fictional, from this page');
  const grab = async (n) => new File([await (await fetch(`./example/${n}`)).blob()], n);
  picked.schedule = await grab('schedule.csv');
  picked.invoices = await grab('invoices.csv');
  $('scheduleName').textContent = 'schedule.csv (example)';
  $('invoicesName').textContent = 'invoices.csv (example)';
  template = await (await fetch('./example/mapping.json')).json();
  buildMappingForm(template); renderRules();
  $('asof').value = '2025-10';
  setStatus('Example loaded. Press Produce the findings.', '');
}

// ---- wiring ----------------------------------------------------------------
(async () => {
  template = await (await fetch('./mapping.example.json')).json();
  buildMappingForm(template); renderRules();
  $('mapfile').onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
      const parsed = JSON.parse(await f.text());
      if (!parsed.schedule || !parsed.invoices) throw new Error('that file has no "schedule" and "invoices" section');
      template = parsed;                       // rules included, which is the point
      buildMappingForm(template); renderRules();
      $('mapfileName').textContent = `${f.name} loaded, rules included`;
    } catch (err) {
      $('mapfileName').textContent = '';
      $('error').hidden = false;
      $('error').textContent = `That mapping could not be read: ${err.message}. Nothing was changed, and the fields below are still the ones you can see.`;
    }
  };
  $('schedule').onchange = (e) => { picked.schedule = e.target.files[0]; $('scheduleName').textContent = picked.schedule?.name ?? ''; };
  $('invoices').onchange = (e) => { picked.invoices = e.target.files[0]; $('invoicesName').textContent = picked.invoices?.name ?? ''; };
  $('run').onclick = run;
  $('example').onclick = loadExample;
  // Warm the runtime NOW, before a file can be chosen. A Playwright run showed
  // all 20 MB arriving after the file-input event when this was lazy, which is
  // the one window the banner promises is silent.
  setStatus('Loading Python', 'about 20 MB from this page, once, before any file is chosen');
  ensureWorker().postMessage({ type: 'warm' });
})();
