const $ = (id) => document.getElementById(id);
const list = $('gallery-list');
const form = $('edit-person');
const photoForm = $('add-photos');
const photoInput = $('new-photos');
let people = [];
let editing = null;
let busy = false;
let undoRemoval = null;

function showMessage(id, text = '', kind = '') {
  const message = $(id);
  message.textContent = text;
  message.dataset.kind = kind;
  message.hidden = !text;
}

async function galleryRequest(path, data) {
  const isUpload = data instanceof Blob;
  const options = data === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': isUpload ? 'application/octet-stream' : 'application/json' },
    body: isUpload ? data : JSON.stringify(data),
  };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The gallery request failed.');
  return result;
}

function renderPeople() {
  const query = $('search-people').value.trim().toLowerCase();
  const matches = people.filter(({ label, fullName }) =>
    label.toLowerCase().includes(query) || fullName.toLowerCase().includes(query));
  list.replaceChildren(...matches.map((person) => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.disabled = busy;
    button.setAttribute('aria-pressed', String(editing?.label === person.label));
    const name = document.createElement('span');
    name.dir = 'auto';
    name.textContent = person.fullName || person.label;
    const summary = document.createElement('span');
    summary.className = 'hint';
    const count = `${person.photoCount} ${person.photoCount === 1 ? 'photo' : 'photos'}`;
    summary.textContent = person.fullName && person.label !== person.fullName ? `${person.label} · ${count}` : count;
    button.append(name, summary);
    button.addEventListener('click', () => choosePerson(person));
    item.append(button);
    return item;
  }));
  if (!busy) showMessage('gallery-state', matches.length ? '' : people.length ? 'No matching people.' : 'No people in the gallery yet.');
}

function choosePerson(person) {
  if (editing?.label !== person?.label) {
    undoRemoval = null;
    photoForm.reset();
    renderPhotoSelection();
    showMessage('photo-state');
  }
  editing = person;
  form.hidden = !person;
  $('editor-empty').hidden = Boolean(person);
  $('person-label').value = person?.label ?? '';
  $('person-name').value = person?.fullName ?? '';
  $('person-notes').value = person?.notes ?? '';
  renderPhotos(person);
  showMessage('edit-state');
  renderPeople();
}

function renderPhotos(person) {
  $('person-photos').hidden = !person;
  const photos = person?.photos ?? [];
  $('photos-empty').hidden = photos.length !== 0;
  $('undo-photo').hidden = !undoRemoval;
  $('reference-photos').replaceChildren(...photos.map((name) => {
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = `/admin/photo?${new URLSearchParams({ label: person.label, name })}`;
    link.target = '_blank';
    link.rel = 'noopener';
    link.setAttribute('aria-label', `Open ${name} at full size`);
    const image = document.createElement('img');
    image.alt = '';
    image.loading = 'lazy';
    image.decoding = 'async';
    image.src = link.href;
    const caption = document.createElement('span');
    caption.textContent = name;
    image.addEventListener('error', () => {
      image.hidden = true;
      caption.textContent = `${name} · Image unavailable`;
    });
    link.append(image, caption);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-photo';
    remove.disabled = busy;
    remove.textContent = 'Remove';
    remove.setAttribute('aria-label', `Remove ${name}`);
    remove.addEventListener('click', () => changePhotos(
      (currentLabel) => galleryRequest('/api/people/photos/remove', { currentLabel, name }),
      'Removing photo…', 'Photo removed. Restart recognition to use the updated gallery.',
    ));
    item.append(link, remove);
    return item;
  }));
}

function setBusy(value) {
  busy = value;
  $('reload-people').disabled = value;
  $('search-people').disabled = value;
  $('person-fields').disabled = value;
  for (const id of ['new-photos', 'pick-photos', 'undo-photo']) $(id).disabled = value;
  renderPhotoSelection();
  for (const button of $('reference-photos').querySelectorAll('button')) button.disabled = value;
  for (const button of list.querySelectorAll('button')) button.disabled = value;
}

async function changePhotos(request, pending, success) {
  if (busy || !editing) return false;
  const label = editing.label;
  setBusy(true);
  showMessage('photo-state', pending);
  try {
    const { person, removed } = await request(label);
    people = people.map((entry) => entry.label === label ? person : entry);
    // Keep the editor's original profile version while it may contain unsaved changes.
    editing = { ...editing, photoCount: person.photoCount, photos: person.photos };
    if (removed !== undefined) undoRemoval = removed;
    renderPhotos(editing);
    showMessage('photo-state', success, 'ok');
    return true;
  } catch (err) {
    showMessage('photo-state', err.message, 'error');
    return false;
  } finally {
    setBusy(false);
    renderPeople();
  }
}

function renderPhotoSelection() {
  const count = photoInput.files.length;
  $('new-photo-count').textContent = count ? `${count} selected` : '';
  $('upload-photos').disabled = busy || !count;
}

$('pick-photos').addEventListener('click', () => photoInput.click());
photoInput.addEventListener('change', renderPhotoSelection);

photoForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = Array.from(photoInput.files);
  if (!files.length) return;
  const saved = await changePhotos(async (currentLabel) => {
    const upload = await referencePhotoUpload(files, { currentLabel });
    showMessage('photo-state', 'Uploading photos…');
    return galleryRequest('/api/people/photos/add', upload);
  }, 'Preparing photos…', 'Photos added. Restart recognition to use the updated gallery.');
  if (saved) {
    photoForm.reset();
    renderPhotoSelection();
  }
});

$('undo-photo').addEventListener('click', () => {
  if (!undoRemoval) return;
  changePhotos((currentLabel) => galleryRequest('/api/people/photos/restore', { currentLabel, ...undoRemoval }),
    'Restoring photo…', 'Photo restored. Restart recognition to use the updated gallery.');
});

async function loadPeople() {
  if (busy) return;
  setBusy(true);
  showMessage('gallery-state', 'Loading the gallery…');
  try {
    ({ people } = await galleryRequest('/api/people'));
    setBusy(false);
    choosePerson(people.find(({ label }) => label === editing?.label) ?? null);
  } catch (err) {
    showMessage('gallery-state', err.message, 'error');
  } finally {
    setBusy(false);
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !editing) return;
  const currentLabel = editing.label;
  const enteredLabel = $('person-label').value;
  const fields = {
    currentLabel, version: editing.version,
    label: enteredLabel === currentLabel ? currentLabel : enteredLabel.trim(),
    fullName: $('person-name').value, notes: $('person-notes').value,
  };
  setBusy(true);
  $('save-person').textContent = 'Saving…';
  showMessage('edit-state');
  try {
    const { person } = await galleryRequest('/api/people/update', fields);
    people = people.map((entry) => entry.label === currentLabel ? person : entry);
    people.sort((a, b) => a.label.localeCompare(b.label));
    choosePerson(person);
    showMessage('edit-state', 'Saved. Recognition will use these details the next time it starts.', 'ok');
  } catch (err) {
    showMessage('edit-state', err.message, 'error');
  } finally {
    $('save-person').textContent = 'Save changes';
    setBusy(false);
    renderPeople();
  }
});

$('search-people').addEventListener('input', renderPeople);
$('reload-people').addEventListener('click', loadPeople);
loadPeople();
