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

/**
 * Check G: the mapping and the as of month are kept, so next month is one click.
 *
 * WHAT IS KEPT, AND IT IS NAMED ON THE PAGE RATHER THAN LEFT TO BE DISCOVERED.
 * The mapping json (which is column NAMES and the rules, never a figure) and the
 * as of month. Nothing else: no schedule, no invoice line, no output, no
 * identifier of any kind. The files themselves are never written anywhere.
 *
 * WHERE. `localStorage`, which is this browser on this machine, same origin,
 * and is not sent with any request. Keeping it does not weaken the zero request
 * claim and check G proves that by measuring it again.
 *
 * WHY WRAPPED. `localStorage` throws outright in a private window and in a
 * browser with site data blocked, and a kit that refuses to run because it
 * could not save a preference would be a worse kit. Every path here degrades to
 * "not saved" and says so.
 */
const STORE_KEY = 'arr-check.mapping.v1';
const DEFAULT_ASOF = '2025-10';
const store = {
  read() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  },
  write(mapping, asof) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ mapping, asof, saved_utc: new Date().toISOString().slice(0, 10) })); return true; }
    catch { return false; }
  },
  clear() { try { localStorage.removeItem(STORE_KEY); return true; } catch { return false; } },
};

function showSaved(note) {
  $('savedNote').textContent = note;
  $('clearSaved').hidden = !note;
}
// pilot.py writes thirteen; the page adds receipt.json and mapping-used.json.
const KIT_FILES = 13;
const ZIP_FILES = KIT_FILES + 2;

/** The kit version as the run itself recorded it, not re-derived from source. */
const kitVersionFromReport = (files) =>
  JSON.parse(new TextDecoder().decode(files['manifest.json'])).kit_version ?? 'unknown';

let template = null;      // mapping.example.json, as loaded
let worker = null;
let picked = { schedule: null, invoices: null, gl: null };
/**
 * Whether the data files in `picked` came from an example button.
 *
 * MEASURED DEFECT, checkGL.mjs: click the Larkspur example, then choose your
 * own schedule and invoices, and the run still used the example's fictional
 * ledger. The report said "Per the general ledger: 800. Difference: -800"
 * against the partner's own data. The same carry-over held for an example
 * schedule or invoice file when only one of the two was replaced. So choosing
 * ANY file of your own drops every data file an example loaded, and the run
 * is either all example or all yours, never a mix.
 */
let fromExample = false;
function leaveExample() {
  if (!fromExample) return;
  picked.schedule = null; picked.invoices = null; picked.gl = null;
  $('scheduleName').textContent = ''; $('invoicesName').textContent = ''; $('glName').textContent = '';
  fromExample = false;
}
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
      window.dispatchEvent(new Event('arr-check-warm'));
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

  // THE RECEIPT IS THE KIT'S, NOT THE PAGE'S. From `db092a6` pilot.py imports
  // receipt.py from its own folder and writes receipt.json itself, so the page
  // ships receipt.py beside pilot.py and adds nothing. Until then the page wrote
  // its own receipt: a second implementation of one file, which had already
  // drifted from the verifier once. `mapping-used.json` is placed in the output
  // folder by the worker BEFORE the run, so the kit's receipt lists it and the
  // zip verifies on any laptop with `--map out/mapping-used.json`.
  //
  // If receipt.json is absent, pilot.py printed why. That is said on screen
  // rather than papered over with a receipt the page made up.
  const hasReceipt = !!files['receipt.json'];
  const names = Object.keys(files).sort();

  // Saved only after a run that worked. Saving a mapping that failed would hand
  // next month a starting point nobody has seen produce a report.
  const saved = store.write(JSON.parse(mappingText), $('asof').value);
  showSaved(saved
    ? `Mapping and month saved in this browser for next time. Column names and rules only, no data.`
    : `Could not save the mapping in this browser, so nothing was saved. The run itself was unaffected.`);

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
  // Counted, not compared to a constant. The kit's file list grows (the bridge
  // and the deferred lane added five), and a hardcoded thirteen would have
  // called every correct run short.
  const zipped = Object.keys(files).length;
  const receiptNote = hasReceipt ? 'receipt.json included' : 'NO receipt.json, see what the run printed below';
  setStatus(`Done in ${(elapsedMs / 1000).toFixed(1)}s`, `${zipped} files in the zip, ${receiptNote}. Nothing was sent anywhere.`);
  $('run').disabled = false;
}

async function run() {
  if (!picked.schedule || !picked.invoices) { setStatus('Two files are needed first', ''); return; }
  $('error').hidden = true; $('download').hidden = true; $('reportWrap').hidden = true;
  $('run').disabled = true;
  setStatus('Reading the files', 'in this tab');
  picked.scheduleBytes = new Uint8Array(await picked.schedule.arrayBuffer());
  picked.invoicesBytes = new Uint8Array(await picked.invoices.arrayBuffer());
  picked.glBytes = picked.gl ? new Uint8Array(await picked.gl.arrayBuffer()) : null;
  const mb = (n) => (n / 1048576).toFixed(1);
  startTicker(`${mb(picked.scheduleBytes.length + picked.invoicesBytes.length)} MB of exports`);
  ensureWorker().postMessage({
    schedule: picked.scheduleBytes.slice().buffer, invoices: picked.invoicesBytes.slice().buffer,
    scheduleName: picked.schedule.name, invoicesName: picked.invoices.name,
    mapping: mappingFromForm(), asof: $('asof').value,
    gl: picked.glBytes ? picked.glBytes.slice().buffer : null,
  });
}

// ---- the example -----------------------------------------------------------
/**
 * The two examples. Same loader, so they cannot drift apart in how they load.
 *
 * Larkspur is the identity test: planted exceptions, every figure stated in the
 * kit's README. QuickBooks is the shape the deferred lane is built for: an
 * invoice export with no service period at all, monthly, quarterly and annual
 * invoices and one discounted annual, with the expected deferred balance stated
 * in arr-check's gen_quickbooks.py. Both are fictional.
 */
const EXAMPLES = {
  // Larkspur carries the fictional ledger, so the page shows the -800 the
  // published example shows. QuickBooks has none, and says so.
  larkspur: { dir: './example', label: 'the Larkspur example', asof: '2025-10', gl: 'deferred_gl.csv' },
  quickbooks: { dir: './example-quickbooks', label: 'the QuickBooks example', asof: '2025-10', gl: null },
};

async function loadExample(which) {
  const ex = EXAMPLES[which];
  setStatus(`Loading ${ex.label}`, 'fictional, from this page');
  const grab = async (n) => new File([await (await fetch(`${ex.dir}/${n}`)).blob()], n);
  picked.schedule = await grab('schedule.csv');
  picked.invoices = await grab('invoices.csv');
  $('scheduleName').textContent = `schedule.csv (${ex.label})`;
  $('invoicesName').textContent = `invoices.csv (${ex.label})`;
  picked.gl = ex.gl ? await grab(ex.gl) : null;
  $('glName').textContent = ex.gl ? `${ex.gl} (${ex.label})` : '';
  $('glfile').value = '';
  // Its own mapping, rules included. Loading only the column names is the
  // defect B found: every run then used the example's rules.
  template = await (await fetch(`${ex.dir}/mapping.json`)).json();
  buildMappingForm(template); renderRules();
  $('asof').value = ex.asof;
  fromExample = true;
  // Clear the inputs' own values too, so a file chosen next always fires a change.
  $('schedule').value = ''; $('invoices').value = '';
  setStatus(`${ex.label[0].toUpperCase()}${ex.label.slice(1)} loaded. Press Produce the findings.`, '');
}

// ---- wiring ----------------------------------------------------------------
(async () => {
  const kept = store.read();
  template = kept && kept.mapping && kept.mapping.schedule
    ? kept.mapping
    : await (await fetch('./mapping.example.json')).json();
  if (kept && kept.asof) $('asof').value = kept.asof;
  if (kept) showSaved(`Using the mapping you saved on ${kept.saved_utc}. Column names and rules only, no data.`);
  buildMappingForm(template); renderRules();
  $('clearSaved').onclick = async (e) => {
    e.preventDefault();
    store.clear();
    template = await (await fetch('./mapping.example.json')).json();
    buildMappingForm(template); renderRules();
    // The month was saved too, so clearing has to take it. The first version
    // left 2025-09 on screen under a message saying the fields were back to the
    // example, which made the message false by one field.
    $('asof').value = DEFAULT_ASOF;
    showSaved('');
    setStatus('Saved mapping cleared', 'The columns, the rules and the month are back to the blank example. Nothing is stored now.');
  };
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
  $('schedule').onchange = (e) => { leaveExample(); picked.schedule = e.target.files[0]; $('scheduleName').textContent = picked.schedule?.name ?? ''; };
  $('glfile').onchange = (e) => { leaveExample(); picked.gl = e.target.files[0] || null; $('glName').textContent = picked.gl?.name ?? ''; };
  $('invoices').onchange = (e) => { leaveExample(); picked.invoices = e.target.files[0]; $('invoicesName').textContent = picked.invoices?.name ?? ''; };
  $('run').onclick = run;
  $('example').onclick = () => loadExample('larkspur');
  $('exampleQb').onclick = () => loadExample('quickbooks');
  // Warm the runtime NOW, before a file can be chosen. A Playwright run showed
  // all 20 MB arriving after the file-input event when this was lazy, which is
  // the one window the banner promises is silent.
  setStatus('Loading Python', 'about 20 MB from this page, once, before any file is chosen');
  // A HOST THAT DOES NOT SEND application/wasm LEAVES THIS ON "Loading Python"
  // FOREVER with only a console warning, which Cowork hit and I did not.
  // GitHub Pages sends the right type, so this is not the expected path; it is
  // here because a silent wait is the one failure a reader cannot diagnose.
  const warmWatch = setTimeout(() => {
    if (pilotSha) return;                     // it warmed; nothing to say
    $('error').hidden = false;
    $('error').textContent =
      'Python has not started after 60 seconds. This usually means the server '
      + 'hosting this page is not sending WebAssembly files as application/wasm. '
      + 'Nothing is wrong with your files, and nothing has been sent anywhere. '
      + 'Try Chrome or Firefox, or open this page from a host that serves .wasm correctly.';
  }, 60000);
  ensureWorker().postMessage({ type: 'warm' });
  const stopWarmWatch = () => clearTimeout(warmWatch);
  window.addEventListener('arr-check-warm', stopWarmWatch, { once: true });
})();
