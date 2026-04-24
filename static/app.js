/* Holy Colours – App Logic */

const defaultConfig = JSON.parse(document.getElementById('default-config').textContent);
const state = {
  presets: [],
  selectedPresetId: '',
  nameColors: { ...(defaultConfig.name_colors || {}) },
  fallbackColors: [...(defaultConfig.fallback_colors || ['#F4CCCC'])],
};

const el = (id) => document.getElementById(id);
const toastEl = el('status-toast');
let toastTimer = null;

/* ---- Status toast ---- */
function setStatus(message, kind = '') {
  clearTimeout(toastTimer);
  toastEl.textContent = message;
  toastEl.className = `status-toast ${kind} visible`.trim();
  toastTimer = setTimeout(() => {
    toastEl.classList.remove('visible');
  }, kind === 'error' ? 6000 : 3500);
}

/* ---- Step indicator ---- */
function activateStep(n) {
  document.querySelectorAll('.step-pill').forEach((pill) => {
    const step = Number(pill.dataset.step);
    pill.classList.toggle('active', step <= n);
  });
}

/* ---- Hex helpers ---- */
function normalizeHex(value) {
  const text = String(value || '').trim();
  if (/^#[0-9a-fA-F]{6}$/.test(text)) return text.toUpperCase();
  return null;
}

/* ---- Config from UI ---- */
function configFromUI() {
  const nameColors = {};
  document.querySelectorAll('#name-colors .color-row').forEach((row) => {
    const name = row.querySelector('[data-name]').value.trim();
    const hex = normalizeHex(row.querySelector('[data-hex]').value);
    if (name && hex) nameColors[name] = hex;
  });
  const fallbackColors = [];
  document.querySelectorAll('#fallback-colors .color-row').forEach((row) => {
    const hex = normalizeHex(row.querySelector('[data-hex]').value);
    if (hex) fallbackColors.push(hex);
  });
  if (!fallbackColors.length) {
    throw new Error('Mindestens eine gültige Fallbackfarbe ist notwendig.');
  }
  return { name_colors: nameColors, fallback_colors: fallbackColors };
}

/* ---- Sync color/hex inputs ---- */
function syncHexInputs(row, color) {
  const colorInput = row.querySelector('[data-color]');
  const hexInput = row.querySelector('[data-hex]');
  colorInput.value = color;
  hexInput.value = color;
  colorInput.addEventListener('input', () => {
    hexInput.value = colorInput.value.toUpperCase();
  });
  hexInput.addEventListener('input', () => {
    const hex = normalizeHex(hexInput.value);
    if (hex) colorInput.value = hex;
  });
  hexInput.addEventListener('blur', () => {
    const hex = normalizeHex(hexInput.value);
    if (hex) hexInput.value = hex;
  });
}

/* ---- Name color rows ---- */
function renderNameRows(nameColors = state.nameColors) {
  const container = el('name-colors');
  container.innerHTML = '';
  const entries = Object.entries(nameColors);
  if (!entries.length) {
    container.innerHTML = '<div class="empty-state">Noch keine Sprecher hinzugefügt.</div>';
    return;
  }
  entries.forEach(([name, color]) =>
    addNameRow(name, normalizeHex(color) || '#FFD966')
  );
}

function addNameRow(name = '', color = '#FFD966') {
  // Remove empty-state if present
  const empty = el('name-colors').querySelector('.empty-state');
  if (empty) empty.remove();

  const row = document.createElement('div');
  row.className = 'color-row';
  row.innerHTML = `
    <input data-name type="text" placeholder="Sprechername">
    <input data-color type="color">
    <input data-hex type="text" inputmode="text" placeholder="#FFD966">
    <button class="btn-icon btn-danger" type="button" title="Entfernen">×</button>
  `;
  row.querySelector('[data-name]').value = name;
  syncHexInputs(row, color);
  row.querySelector('button').addEventListener('click', () => {
    row.style.opacity = '0';
    row.style.transform = 'translateY(-8px)';
    row.style.transition = 'all .2s ease';
    setTimeout(() => {
      row.remove();
      if (!el('name-colors').querySelector('.color-row')) {
        el('name-colors').innerHTML =
          '<div class="empty-state">Noch keine Sprecher hinzugefügt.</div>';
      }
    }, 200);
  });
  el('name-colors').appendChild(row);
  // Activate step 2 when editing colors
  activateStep(2);
}

/* ---- Fallback color rows ---- */
function renderFallbackRows(colors = state.fallbackColors) {
  const container = el('fallback-colors');
  container.innerHTML = '';
  const list = colors.length ? colors : ['#F4CCCC'];
  list.forEach((color) => addFallbackRow(normalizeHex(color) || '#F4CCCC'));
}

function addFallbackRow(color = '#F4CCCC') {
  const row = document.createElement('div');
  row.className = 'color-row fallback-grid';
  row.innerHTML = `
    <input data-color type="color">
    <input data-hex type="text" inputmode="text" placeholder="#F4CCCC">
    <button class="btn-icon btn-danger" type="button" title="Entfernen">×</button>
  `;
  syncHexInputs(row, color);
  row.querySelector('button').addEventListener('click', () => {
    row.style.opacity = '0';
    row.style.transform = 'translateY(-8px)';
    row.style.transition = 'all .2s ease';
    setTimeout(() => row.remove(), 200);
  });
  el('fallback-colors').appendChild(row);
}

/* ---- Preset select ---- */
function renderPresetSelect() {
  const select = el('preset-select');
  select.innerHTML = '<option value="">Aktuelle Einstellungen</option>';
  state.presets.forEach((preset) => {
    const option = document.createElement('option');
    option.value = preset.id;
    option.textContent = preset.name;
    select.appendChild(option);
  });
  select.value = state.selectedPresetId;
}

function applyConfig(config) {
  state.nameColors = { ...(config.name_colors || {}) };
  state.fallbackColors = [...(config.fallback_colors || ['#F4CCCC'])];
  renderNameRows();
  renderFallbackRows();
}

/* ---- API calls ---- */
async function loadPresets() {
  const response = await fetch('/api/presets');
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || 'Presets konnten nicht geladen werden.');
  state.presets = data.presets || [];
  renderPresetSelect();
}

async function savePreset() {
  const config = configFromUI();
  const name = el('preset-name').value.trim();
  if (!name) throw new Error('Bitte einen Preset-Namen eingeben.');
  const response = await fetch('/api/presets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: state.selectedPresetId || undefined,
      name,
      ...config,
    }),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || 'Preset konnte nicht gespeichert werden.');
  state.selectedPresetId = data.preset.id;
  await loadPresets();
  setStatus('Preset gespeichert.', 'ok');
}

async function deletePreset() {
  if (!state.selectedPresetId)
    throw new Error('Kein Preset ausgewählt.');

  // Show confirm dialog
  const confirmed = await showConfirm(
    'Preset wirklich löschen? Diese Aktion kann nicht rückgängig gemacht werden.'
  );
  if (!confirmed) return;

  const response = await fetch(
    `/api/presets/${encodeURIComponent(state.selectedPresetId)}`,
    { method: 'DELETE' }
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || 'Preset konnte nicht gelöscht werden.');
  state.selectedPresetId = '';
  el('preset-name').value = '';
  await loadPresets();
  setStatus('Preset gelöscht.', 'ok');
}

/* ---- Confirm dialog ---- */
function showConfirm(message) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `
      <div class="confirm-box">
        <p>${message}</p>
        <div class="btn-row">
          <button class="cancel-btn" type="button">Abbrechen</button>
          <button class="confirm-btn btn-danger" type="button">Löschen</button>
        </div>
      </div>
    `;
    overlay.querySelector('.cancel-btn').addEventListener('click', () => {
      overlay.remove();
      resolve(false);
    });
    overlay.querySelector('.confirm-btn').addEventListener('click', () => {
      overlay.remove();
      resolve(true);
    });
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) {
        overlay.remove();
        resolve(false);
      }
    });
    document.body.appendChild(overlay);
  });
}

/* ---- File processing ---- */
async function processFile() {
  const file = el('docx-file').files[0];
  if (!file) throw new Error('Bitte eine DOCX-Datei auswählen.');

  const processBtn = el('process');
  const processLabel = el('process-label');
  processBtn.disabled = true;
  processLabel.innerHTML = '<span class="spinner"></span> PDF wird erstellt …';
  activateStep(3);

  try {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('config', JSON.stringify(configFromUI()));

    const response = await fetch('/api/process', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || 'PDF konnte nicht erstellt werden.');
    }

    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match
      ? match[1]
      : file.name.replace(/\.docx$/i, '.colored.pdf');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setStatus('PDF bereit! Download gestartet.', 'ok');
  } finally {
    processBtn.disabled = false;
    processLabel.textContent = 'PDF erstellen';
  }
}

/* ---- Drag & drop ---- */
function setupDropzone() {
  const dropzone = el('dropzone');
  const fileInput = el('docx-file');
  const infoContainer = el('file-info-container');

  function showFileInfo(file) {
    infoContainer.innerHTML = `
      <div class="file-info">
        <span class="file-icon">📄</span>
        <span class="file-name">${file.name}</span>
        <button class="file-remove" type="button" title="Datei entfernen">×</button>
      </div>
    `;
    infoContainer.querySelector('.file-remove').addEventListener('click', () => {
      fileInput.value = '';
      infoContainer.innerHTML = '';
    });
    activateStep(3);
  }

  ['dragenter', 'dragover'].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    })
  );

  ['dragleave', 'drop'].forEach((evt) =>
    dropzone.addEventListener(evt, () => dropzone.classList.remove('dragover'))
  );

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file && file.name.toLowerCase().endsWith('.docx')) {
      // Create a new DataTransfer to set the file input
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      showFileInfo(file);
    } else {
      setStatus('Bitte eine .docx-Datei verwenden.', 'error');
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) {
      showFileInfo(fileInput.files[0]);
    }
  });
}

/* ---- Error wrapper ---- */
function run(action) {
  Promise.resolve()
    .then(action)
    .catch((error) => setStatus(error.message || String(error), 'error'));
}

/* ---- Event listeners ---- */
el('add-name').addEventListener('click', () => addNameRow());
el('add-fallback').addEventListener('click', () => addFallbackRow());

el('new-preset').addEventListener('click', () => {
  state.selectedPresetId = '';
  el('preset-select').value = '';
  el('preset-name').value = '';
  applyConfig(defaultConfig);
  setStatus('Neues Preset angelegt.');
  activateStep(1);
});

el('save-preset').addEventListener('click', () => run(savePreset));
el('delete-preset').addEventListener('click', () => run(deletePreset));
el('process').addEventListener('click', () => run(processFile));

el('preset-select').addEventListener('change', (event) => {
  state.selectedPresetId = event.target.value;
  const preset = state.presets.find(
    (item) => item.id === state.selectedPresetId
  );
  if (preset) {
    el('preset-name').value = preset.name;
    applyConfig(preset);
    activateStep(2);
  }
});

/* ---- Init ---- */
setupDropzone();
applyConfig(defaultConfig);
run(loadPresets);
