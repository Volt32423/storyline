/**
 * Storyline: Google Sheet bridge.
 *
 * This small script lives inside the Google Sheet and lets the Storyline app
 * read and write rows over HTTPS. No Google Cloud project or service account
 * key is needed.
 *
 * Setup (once):
 *   1. In the Sheet: Extensions > Apps Script. Replace everything in Code.gs
 *      with this file.
 *   2. Change SECRET below to a long random string. Put the exact same string
 *      in SHEETS_WEBAPP_SECRET on Render.
 *   3. Deploy > New deployment > Select type: Web app.
 *      Execute as: Me.  Who has access: Anyone.  Click Deploy and allow access.
 *   4. Copy the Web app URL (it ends in /exec) into SHEETS_WEBAPP_URL on Render.
 *
 * If you edit this script later, the change only goes live after
 * Deploy > Manage deployments > (pencil) > Version: New version > Deploy.
 */

const SECRET = 'PASTE-YOUR-SECRET-HERE';

function doGet() {
  // Open the /exec URL in a browser to check the bridge is deployed.
  return reply_({ ok: true, message: 'Storyline sheet bridge is running.' });
}

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (SECRET.indexOf('PASTE-') === 0 || req.secret !== SECRET) {
      return reply_({ ok: false, error: 'unauthorized: check SHEETS_WEBAPP_SECRET matches SECRET in the script' });
    }
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    switch (req.action) {
      case 'ping':
        return reply_({ ok: true });
      case 'ensure_tabs':
        return reply_(withLock_(function () { return ensureTabs_(ss, req.tabs); }));
      case 'append':
        return reply_(withLock_(function () { return append_(ss, req.tab, req.values); }));
      case 'rows':
        return reply_(rows_(ss, req.tab));
      case 'update_cell':
        return reply_(withLock_(function () { return updateCell_(ss, req.tab, req.row, req.col, req.value); }));
      default:
        return reply_({ ok: false, error: 'unknown action: ' + req.action });
    }
  } catch (err) {
    return reply_({ ok: false, error: String(err) });
  }
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function withLock_(fn) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    return fn();
  } finally {
    lock.releaseLock();
  }
}

// Text that starts with = + or @ gets a leading apostrophe, so Sheets stores
// it as plain text and never runs it as a formula. The apostrophe isn't shown.
function safe_(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return /^[=+@]/.test(s) ? "'" + s : s;
}

function sheet_(ss, tab) {
  const sheet = ss.getSheetByName(tab);
  if (!sheet) throw new Error('no tab named ' + tab);
  return sheet;
}

function ensureTabs_(ss, tabs) {
  Object.keys(tabs).forEach(function (name) {
    const headers = tabs[name];
    let sheet = ss.getSheetByName(name);
    if (!sheet) sheet = ss.insertSheet(name);
    const range = sheet.getRange(1, 1, 1, headers.length);
    const current = range.getDisplayValues()[0];
    if (current.join('\u0001') !== headers.join('\u0001')) {
      range.setValues([headers]);
      sheet.setFrozenRows(1);
    }
  });
  return { ok: true };
}

function append_(ss, tab, values) {
  const sheet = sheet_(ss, tab);
  sheet.appendRow(values.map(safe_));
  return { ok: true, row: sheet.getLastRow() };
}

function rows_(ss, tab) {
  // Display values come back as text, but appendRow has already turned
  // number-like text into numbers ("0412" reads back as "412"), so the app
  // pads phone_last4 back to 4 digits before comparing.
  return { ok: true, values: sheet_(ss, tab).getDataRange().getDisplayValues() };
}

function updateCell_(ss, tab, row, col, value) {
  sheet_(ss, tab).getRange(row, col).setValue(safe_(value));
  return { ok: true };
}
