const $ = (id) => document.getElementById(id);
const statusText = {
  idle: 'Idle', starting: 'Starting', running: 'Running', stopping: 'Stopping',
  error: 'Session failed', offline: 'Disconnected',
};
let acting = false;
let actionRevision = 0;
let lastStatus = { state: 'offline', preview: false, revision: 0 };
let previewTimer = null;
let previewRevision = null;
let previewAbort = null;
let previewObjectUrl = null;
let displayedProfiles = '';
const hosted = location.protocol === 'https:';
const ipField = $('jetson-ip');
const ipPreference = 'sentinal-eye.jetson-ip';
const offlineMessage = hosted ? 'Cannot reach the Jetson. Check that it is on and connected to the network.'
  : 'Cannot reach the control panel. Check that its Terminal window is still open.';
$('ssh-target').hidden = hosted;
$('sign-out').hidden = !hosted;
$('people-admin').hidden = !hosted;

try {
  ipField.value = localStorage.getItem(ipPreference) || ipField.value;
} catch {}

function validIp(ip) {
  if (ip.includes(':')) {
    if (!/^[0-9a-f:.]+$/i.test(ip)) return false;
    try {
      new URL(`http://[${ip}]/`);
      return true;
    } catch {
      return false;
    }
  }
  const parts = ip.split('.');
  return parts.length === 4 && parts.every((part) => /^(0|[1-9][0-9]{0,2})$/.test(part) && Number(part) <= 255);
}

function rememberIp() {
  const ip = ipField.value.trim();
  if (!validIp(ip)) return;
  try {
    localStorage.setItem(ipPreference, ip);
  } catch {}
}

ipField.addEventListener('change', rememberIp);

function selectedIp(result) {
  const ip = ipField.value.trim();
  if (validIp(ip)) return ip;
  message(result, 'Enter a valid Jetson IP address.', 'error');
  ipField.focus();
  return null;
}

async function api(path, body, type = 'application/json') {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': type }, body };
  let response;
  try {
    response = await fetch(`/api/${path}`, options);
  } catch {
    throw new Error(offlineMessage);
  }
  if (response.status === 401) {
    if (hosted) location.replace('/login');
    throw new Error('Sign in to Sentinal Eye and try again.');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}

function message(element, text, kind = '') {
  element.textContent = text;
  element.dataset.kind = kind;
  element.hidden = !text;
}

function clearPreview() {
  clearTimeout(previewTimer);
  previewAbort?.abort();
  if (previewObjectUrl !== null) URL.revokeObjectURL(previewObjectUrl);
  previewObjectUrl = null;
  previewRevision = null;
  $('preview').removeAttribute('src');
  $('preview').hidden = true;
  $('preview-empty').hidden = false;
}

async function requestPreview(revision) {
  if (previewRevision !== revision) return;
  if (!hosted) {
    $('preview').src = 'http://127.0.0.1:8765/stream.mjpg';
    return;
  }
  const signal = previewAbort.signal;
  try {
    const response = await fetch('/frame.jpg', { signal });
    if (!response.ok) throw new Error('No camera frame');
    const frame = await response.blob();
    if (signal.aborted) return;
    const previous = previewObjectUrl;
    previewObjectUrl = URL.createObjectURL(frame);
    $('preview').src = previewObjectUrl;
    if (previous !== null) URL.revokeObjectURL(previous);
  } catch {
    if (!signal.aborted) previewTimer = setTimeout(() => requestPreview(revision), 1000);
  }
}

$('preview').addEventListener('load', () => {
  if (previewRevision === null) return;
  $('preview').hidden = false;
  $('preview-empty').hidden = true;
  if (hosted) {
    const revision = previewRevision;
    previewTimer = setTimeout(() => requestPreview(revision), document.hidden ? 1000 : 125);
  }
});
$('preview').addEventListener('error', () => {
  if (previewRevision === null) return;
  const revision = previewRevision;
  $('preview').removeAttribute('src');
  $('preview').hidden = true;
  $('preview-empty').hidden = false;
  $('preview-message').textContent = 'Waiting for camera video…';
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => requestPreview(revision), 1500);
});

function renderPeople({ state, personEvents, people = [] }) {
  const profiles = state === 'running' ? people : [];
  const empty = $('people-empty');
  const list = $('recognized-people');
  empty.hidden = profiles.length > 0;
  list.hidden = !profiles.length;
  empty.textContent = state !== 'running' ? 'Waiting for a camera session.'
    : personEvents ? 'No known people detected recently.'
    : 'Update live_insightface.py on the Jetson to display person details.';

  const profileContent = JSON.stringify(profiles);
  if (profileContent === displayedProfiles) return;
  displayedProfiles = profileContent;
  list.replaceChildren(...profiles.map(({ label, fullName, notes, matchPercent }) => {
    const card = document.createElement('li');
    const name = document.createElement('h4');
    name.dir = 'auto';
    name.textContent = fullName || label;
    card.append(name);
    if (fullName && fullName !== label) {
      const tag = document.createElement('p');
      tag.className = 'person-label';
      tag.textContent = label;
      card.append(tag);
    }
    if (Number.isFinite(matchPercent)) {
      const match = document.createElement('p');
      match.className = 'person-match';
      match.textContent = `${matchPercent}% match`;
      card.append(match);
    }
    const note = document.createElement('p');
    note.className = 'person-notes';
    note.dir = 'auto';
    note.textContent = notes || 'No notes on file.';
    card.append(note);
    return card;
  }));
}

function renderPerformance({ state, jetsonStats, recognitionRates }) {
  const connected = state !== 'offline';
  const stats = connected && jetsonStats ? jetsonStats : {};
  const rates = state === 'running' && recognitionRates ? recognitionRates : {};
  const percent = (value) => Number.isFinite(value) ? `${value}%` : '—';
  const fps = (value) => Number.isFinite(value) ? `${value.toFixed(1)} FPS` : '—';
  $('jetson-metrics').hidden = !hosted;
  $('jetson-metrics-state').hidden = !hosted || Boolean(connected && jetsonStats);
  const values = {
    'cpu-load': percent(stats.cpuPercent),
    'gpu-load': percent(stats.gpuPercent),
    'ram-used': Number.isFinite(stats.ramUsedMb) ? `${stats.ramUsedMb} / ${stats.ramTotalMb} MB` : '—',
    'peak-temperature': Number.isFinite(stats.temperatureC) ? `${stats.temperatureC.toFixed(1)} °C` : '—',
    'sensor-0-fps': fps(rates.detectionFps?.['0']),
    'sensor-1-fps': fps(rates.detectionFps?.['1']),
    'preview-fps': fps(rates.previewFps),
  };
  for (const [id, text] of Object.entries(values)) {
    if ($(id).textContent !== text) $(id).textContent = text;
  }
  $('peak-temperature').title = stats.temperatureSensor || '';
}

function render(data) {
  lastStatus = data;
  const { state, preview, revision, logs, ip } = data;
  const active = ['starting', 'running', 'stopping'].includes(state);
  const badge = $('run-status');
  const previous = badge.dataset.state;
  badge.dataset.state = state;
  if (badge.textContent !== statusText[state]) badge.textContent = statusText[state];
  $('start').disabled = acting || active || state === 'offline';
  $('stop').disabled = acting || !active || state === 'stopping';
  for (const id of ['face-model', 'expression-model', 'mode']) $(id).disabled = active || acting;
  if (active && ip) ipField.value = ip;
  ipField.disabled = active || acting || $('enroll').disabled;

  if (logs?.length) {
    const logBox = $('logs');
    const text = logs.join('\n');
    if (logBox.textContent !== text) {
      const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 28;
      logBox.textContent = text;
      if (atBottom) logBox.scrollTop = logBox.scrollHeight;
    }
  }
  if ((state === 'error' || (state === 'running' && !preview)) && previous !== state) $('log-panel').open = true;
  renderPeople(data);
  renderPerformance(data);

  if (preview) {
    if (previewRevision === revision) return;
    clearPreview();
    $('preview-message').textContent = 'Waiting for camera video…';
    previewRevision = revision;
    previewAbort = new AbortController();
    requestPreview(revision);
  } else {
    if (previewRevision !== null) clearPreview();
    $('preview-message').textContent = {
      starting: hosted ? 'Starting recognition on the Jetson…' : 'Connecting to the Jetson. macOS may prompt for your SSH password.',
      running: 'Logs only', error: 'Video unavailable. See the session log.',
      offline: offlineMessage,
    }[state] || 'Start a session to view live video.';
  }
}

async function poll() {
  if (!acting) {
    const currentAction = actionRevision;
    try {
      const data = await api('status');
      if (!acting && currentAction === actionRevision) render(data);
    } catch {
      if (!acting && currentAction === actionRevision) render({ ...lastStatus, state: 'offline', preview: false });
    }
  }
  setTimeout(poll, 1000);
}

async function runAction(action, body) {
  if (acting) return;
  acting = true;
  actionRevision += 1;
  message($('run-result'), '');
  render(lastStatus);
  try {
    lastStatus = await api(action, body);
  } catch (err) {
    message($('run-result'), err.message, 'error');
  } finally {
    acting = false;
    render(lastStatus);
  }
}

$('camera-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const ip = hosted ? undefined : selectedIp($('run-result'));
  if (!hosted && !ip) return;
  runAction('start', JSON.stringify({ ip, face: $('face-model').value, expression: $('expression-model').value, mode: $('mode').value }));
});
$('stop').addEventListener('click', () => runAction('stop', ''));

$('photos').addEventListener('change', () => {
  const count = $('photos').files.length;
  $('photo-count').textContent = count === 1 ? '1 photo selected' : count ? `${count} photos selected` : '';
});

$('person-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('enroll');
  if (button.disabled) return;
  const label = $('label').value.trim();
  const files = Array.from($('photos').files);
  const result = $('enroll-result');
  if (label.toLowerCase() === 'unknown') {
    message(result, 'Choose another label. Unknown is reserved for faces without a match.', 'error');
    return;
  }
  const ip = hosted ? undefined : selectedIp(result);
  if (!hosted && !ip) return;
  const manifest = { ip, label, fullName: $('full-name').value, notes: $('notes').value };
  button.disabled = true;
  ipField.disabled = true;
  button.textContent = 'Preparing photos…';
  message(result, '');
  try {
    const body = await referencePhotoUpload(files, manifest);
    button.textContent = 'Uploading…';
    message(result, hosted ? 'Uploading to the Jetson gallery…' : 'macOS may prompt for your SSH password.');
    const saved = await api('enroll', body, 'application/octet-stream');
    event.target.reset();
    $('photo-count').textContent = '';
    const next = ['running', 'starting'].includes(lastStatus.state)
      ? ' Restart the camera session to load the updated gallery.' : ' The gallery will update on the next start.';
    const photoCount = saved.photos === 1 ? '1 photo' : `${saved.photos} photos`;
    const savedTo = hosted ? `Saved ${saved.label} with ${photoCount} on the Jetson.`
      : `Saved ${saved.label} on the Mac and uploaded ${photoCount} to the Jetson.`;
    message(result, savedTo + next, 'ok');
  } catch (err) {
    message(result, err.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Save and upload';
    render(lastStatus);
  }
});

poll();
