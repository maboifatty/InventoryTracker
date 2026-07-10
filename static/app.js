const $ = (s) => document.querySelector(s);
const body = $('#itemsBody'), empty = $('#emptyState'), form = $('#itemForm');
const dialog = $('#itemDialog'), deleteDialog = $('#deleteDialog'), authForm = $('#authForm');
const sevaDialog = $('#sevaDialog'), sevaForm = $('#sevaForm'), sevaItems = $('#sevaItems');
const inventoryEditDialog = $('#inventoryEditDialog'), inventoryEditForm = $('#inventoryEditForm'), inventoryEditItems = $('#inventoryEditItems');
const sevaLogDialog = $('#sevaLogDialog'), sevaLog = $('#sevaLog');
const sevaEditDialog = $('#sevaEditDialog'), sevaEditForm = $('#sevaEditForm'), sevaEditList = $('#sevaEditList'), sevaEditPanel = $('#sevaEditPanel');
let items = [], sevas = [], editingId = null, deletingId = null, editingSevaId = null, sevaPanelMode = 'none', timer, creatingUser = false;
let authToken = localStorage.getItem('inventoryToken') || '';
let signedInName = localStorage.getItem('inventoryUser') || '';
const number = new Intl.NumberFormat('en-US');

function esc(value='') { const el=document.createElement('span'); el.textContent=value; return el.innerHTML; }
function apiOptions(options={}) {
  return {...options, headers: {'Content-Type': 'application/json', ...(authToken ? {Authorization: `Bearer ${authToken}`} : {}), ...(options.headers || {})}};
}
function stockStatus(item) {
  if (item.quantity === 0) return ['Out of stock','out'];
  if (item.quantity <= item.reorder_level) return ['Low stock','low'];
  return ['In stock','ok'];
}
function showApp() {
  $('#authScreen').hidden = true;
  document.querySelectorAll('.app-only').forEach(el => el.hidden = false);
  $('#signedInUser').textContent = signedInName ? `Signed in as ${signedInName}` : '';
  loadItems();
}
function showLogin() {
  authToken = ''; signedInName = '';
  localStorage.removeItem('inventoryToken'); localStorage.removeItem('inventoryUser');
  $('#authScreen').hidden = false;
  document.querySelectorAll('.app-only').forEach(el => el.hidden = true);
}
async function loadItems() {
  const params = new URLSearchParams({search: $('#search').value, status: $('#statusFilter').value});
  try {
    const res = await fetch(`/api/items?${params}`, apiOptions()), data = await res.json();
    if (res.status === 401) { showLogin(); toast('Please log in.'); return; }
    items = data.items; render(data.stats);
  } catch { toast('Could not connect to the inventory service.'); }
}
function render(stats) {
  $('#totalItems').textContent = number.format(stats.total_items);
  $('#lowStock').textContent = number.format(stats.low_stock);
  renderRestockEmailNote(stats);
  $('#resultCount').textContent = `${items.length} product${items.length === 1 ? '' : 's'}`;
  body.innerHTML = items.map(item => { const status=stockStatus(item); return `<tr>
    <td><div class="product"><strong>${esc(item.name)}</strong><small>${esc(item.sku)}</small></div></td>
    <td><span class="qty">${number.format(item.quantity)}</span></td>
    <td><span class="badge ${status[1]}">${status[0]}</span></td>
    <td class="row-actions"><button class="action-btn" data-edit="${item.id}" title="Edit item">Edit</button><button class="action-btn" data-delete="${item.id}" title="Delete item">Delete</button></td></tr>`; }).join('');
  empty.hidden = items.length !== 0; body.closest('table').hidden = items.length === 0;
}
function renderRestockEmailNote(stats) {
  const note = $('#restockEmailNote');
  if (!stats.low_stock) {
    note.hidden = true;
    note.textContent = '';
    return;
  }
  note.hidden = false;
  note.textContent = stats.restock_email_sent
    ? `Email sent to ${stats.restock_email_to} with restock information.`
    : `Restock email to ${stats.restock_email_to} is pending email setup.`;
}
function showNotificationResult(notification) {
  if (!notification || !notification.message) return;
  toast(notification.message);
}
function openForm(item=null) {
  editingId = item?.id || null; form.reset(); clearErrors(form);
  $('#dialogTitle').textContent = item ? 'Edit inventory item' : 'Add inventory item';
  $('#saveItem').textContent = item ? 'Save changes' : 'Save item';
  if (item) Object.keys(item).forEach(k => { if (form.elements[k]) form.elements[k].value = item[k]; });
  else { form.quantity.value=0; form.reorder_level.value=0; }
  dialog.showModal(); setTimeout(() => form.name.focus(), 30);
}
function openSevaForm() {
  sevaForm.reset(); clearErrors(sevaForm);
  sevaForm.seva_date.valueAsDate = new Date();
  renderSevaItems();
  sevaDialog.showModal(); setTimeout(() => sevaForm.name.focus(), 30);
}
function renderSevaItems() {
  if (!items.length) {
    sevaItems.innerHTML = '<div class="seva-empty">No inventory items available. Add inventory items first.</div>';
    return;
  }
  sevaItems.innerHTML = items.map(item => `<label class="seva-item">
    <span><strong>${esc(item.name)}</strong><small>${number.format(item.quantity)} in stock</small></span>
    <input name="item_${item.id}" data-item-id="${item.id}" type="number" min="0" max="${item.quantity}" step="1" value="0" aria-label="${esc(item.name)} quantity used">
    <small class="error" data-error-for="item_${item.id}"></small>
  </label>`).join('');
}
function openInventoryEditForm() {
  clearErrors(inventoryEditForm);
  renderInventoryEditItems();
  inventoryEditDialog.showModal();
}
function renderInventoryEditItems() {
  if (!items.length) {
    inventoryEditItems.innerHTML = '<div class="seva-empty">No inventory items available. Add inventory items first.</div>';
    return;
  }
  inventoryEditItems.innerHTML = items.map(item => `<div class="inventory-edit-row">
    <div class="product"><strong>${esc(item.name)}</strong><small>${esc(item.sku)}</small></div>
    <label><span>Stock</span><input name="quantity_${item.id}" data-edit-id="${item.id}" data-field="quantity" type="number" min="0" step="1" value="${item.quantity}"><small class="error" data-error-for="quantity_${item.id}"></small></label>
    <label><span>Minimum</span><input name="reorder_${item.id}" data-edit-id="${item.id}" data-field="reorder_level" type="number" min="0" step="1" value="${item.reorder_level}"><small class="error" data-error-for="reorder_${item.id}"></small></label>
  </div>`).join('');
}
async function openSevaLog() {
  sevaLog.innerHTML = '<div class="seva-empty">Loading sevas…</div>';
  sevaLogDialog.showModal();
  try {
    const res = await fetch('/api/sevas', apiOptions());
    const data = await res.json();
    if(res.status === 401){ sevaLogDialog.close(); showLogin(); toast('Please log in.'); return; }
    if(!res.ok){ sevaLog.innerHTML = '<div class="seva-empty">Could not load sevas.</div>'; return; }
    renderSevaLog(data.sevas || []);
  } catch {
    sevaLog.innerHTML = '<div class="seva-empty">Could not connect to the inventory service.</div>';
  }
}
function renderSevaLog(sevas) {
  if (!sevas.length) {
    sevaLog.innerHTML = '<div class="seva-empty">No sevas have been recorded yet.</div>';
    return;
  }
  sevaLog.innerHTML = sevas.map(seva => `<article class="seva-log-entry">
    <div class="seva-log-head"><div><strong>${esc(formatDate(seva.seva_date))} · ${esc(formatSevaType(seva.seva_type))}</strong><small>${esc(seva.name)}${seva.location ? ` · ${esc(seva.location)}` : ''}</small></div><span>${number.format(seva.items.length)} item${seva.items.length === 1 ? '' : 's'}</span></div>
    <ul>${seva.items.map(item => `<li><span>${esc(item.name)}</span><strong>${number.format(item.quantity_used)}</strong></li>`).join('')}</ul>
  </article>`).join('');
}
async function loadSevas() {
  const res = await fetch('/api/sevas', apiOptions());
  const data = await res.json();
  if(res.status === 401){ showLogin(); throw new Error('login'); }
  if(!res.ok) throw new Error(data.error || 'Could not load sevas.');
  sevas = data.sevas || [];
  return sevas;
}
async function openSevaEdit() {
  editingSevaId = null; sevaPanelMode = 'none'; clearErrors(sevaEditForm);
  sevaEditList.innerHTML = '<div class="seva-empty">Loading sevas…</div>';
  sevaEditPanel.innerHTML = '<div class="seva-empty">Select a seva to edit, or add a new seva.</div>';
  $('#saveSevaEdit').disabled = true; $('#deleteSeva').disabled = true;
  sevaEditDialog.showModal();
  try {
    await loadItems();
    await loadSevas();
    renderSevaEditList();
  } catch {
    sevaEditList.innerHTML = '<div class="seva-empty">Could not load sevas.</div>';
  }
}
function renderSevaEditList() {
  if (!sevas.length) {
    sevaEditList.innerHTML = '<div class="seva-empty">No sevas have been recorded yet.</div>';
    return;
  }
  sevaEditList.innerHTML = sevas.map(seva => `<button type="button" class="seva-edit-choice ${seva.id === editingSevaId ? 'selected' : ''}" data-seva-choice="${seva.id}">
    <strong>${esc(formatDate(seva.seva_date))}</strong>
    <small>${esc(formatSevaType(seva.seva_type))}</small>
  </button>`).join('');
}
function openAddSevaInMaster() {
  editingSevaId = null; sevaPanelMode = 'add'; clearErrors(sevaEditForm); renderSevaEditList();
  renderSevaDetailPanel({name:'', location:'', seva_type:'lunch', seva_date: todayValue(), items: []}, 'add');
  $('#saveSevaEdit').disabled = false; $('#deleteSeva').disabled = true;
}
function openSelectedSeva(sevaId) {
  editingSevaId = sevaId; sevaPanelMode = 'edit'; clearErrors(sevaEditForm); renderSevaEditList();
  const seva = sevas.find(entry => entry.id === sevaId);
  if (!seva) return;
  renderSevaDetailPanel(seva, 'edit');
  $('#saveSevaEdit').disabled = false; $('#deleteSeva').disabled = false;
}
function renderSevaDetailPanel(seva, mode) {
  const oldById = Object.fromEntries((seva.items || []).map(item => [item.id, item.quantity_used]));
  sevaEditPanel.innerHTML = `<div class="form-grid seva-edit-fields">
    <label>Seva Name <span>*</span><input name="name" maxlength="120" required value="${esc(seva.name)}"><small class="error"></small></label>
    <label>Seva Date <span>*</span><input name="seva_date" type="date" required value="${esc(seva.seva_date)}"><small class="error"></small></label>
    <label>Type <span>*</span><select name="seva_type" required><option value="breakfast" ${seva.seva_type === 'breakfast' ? 'selected' : ''}>Breakfast</option><option value="lunch" ${seva.seva_type === 'lunch' || !seva.seva_type ? 'selected' : ''}>Lunch</option></select><small class="error"></small></label>
    <label class="full">Location<input name="location" maxlength="160" value="${esc(seva.location || '')}" placeholder="Optional location"><small class="error"></small></label>
    <div class="full seva-items-field">
      <div class="seva-items-head"><strong>Inventory items used</strong><small>${mode === 'add' ? 'Enter quantities used in this seva.' : 'Adjust quantities used in this seva.'}</small></div>
      <div class="seva-items">${items.map(item => {
        const oldQuantity = oldById[item.id] || 0;
        const available = item.quantity + oldQuantity;
        return `<label class="seva-item">
          <span><strong>${esc(item.name)}</strong><small>${number.format(available)} available including this seva</small></span>
          <input name="item_${item.id}" data-edit-seva-item-id="${item.id}" type="number" min="0" max="${available}" step="1" value="${oldQuantity}" aria-label="${esc(item.name)} quantity used">
          <small class="error" data-error-for="item_${item.id}"></small>
        </label>`;
      }).join('')}</div>
      <small class="error" data-error-for="items"></small>
    </div>
  </div>`;
}
function sevaPayloadFrom(scope, selector='[data-item-id]') {
  const itemInputs = [...scope.querySelectorAll(selector)];
  return {
    name: scope.name.value,
    location: scope.location?.value || '',
    seva_type: scope.seva_type?.value || '',
    seva_date: scope.seva_date.value,
    items: itemInputs.map(input => ({id: Number(input.dataset.itemId || input.dataset.editSevaItemId), quantity: Number(input.value || 0)}))
  };
}
function todayValue() {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
function formatSevaType(value='') {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : 'Unspecified';
}
function formatDate(value) {
  if (!value) return '';
  const [year, month, day] = value.split('-').map(Number);
  if (!year || !month || !day) return value;
  return new Intl.DateTimeFormat('en-US', {month:'short', day:'numeric', year:'numeric'}).format(new Date(year, month - 1, day));
}
function clearErrors(scope=document) { scope.querySelectorAll('.error').forEach(e=>e.textContent=''); scope.querySelectorAll('.invalid').forEach(e=>e.classList.remove('invalid')); }
function showErrors(scope, fields={}) { clearErrors(scope); Object.entries(fields || {}).forEach(([name,msg])=>{ const input=scope.elements[name]; const custom=scope.querySelector(`[data-error-for="${name}"]`); if(input){input.classList.add('invalid');input.closest('label').querySelector('.error').textContent=msg;} else if(custom){custom.textContent=msg;} }); }
function toast(message) { const el=$('#toast'); el.textContent=message; el.classList.add('show'); clearTimeout(timer); timer=setTimeout(()=>el.classList.remove('show'),2800); }
function setAuthMode(create) {
  creatingUser = create; clearErrors(authForm); authForm.reset();
  $('#authMode').textContent = create ? 'NEW USER' : 'WELCOME BACK';
  $('#authTitle').textContent = create ? 'Create user' : 'Log in';
  $('#authCopy').textContent = create ? 'Create a user name and password for this local inventory app.' : 'Enter your user name and password to open the inventory overview.';
  $('#authSubmit').textContent = create ? 'Create user' : 'Log in';
  $('#authSwitchText').textContent = create ? 'Already have a user?' : 'New here?';
  $('#switchAuth').textContent = create ? 'Log in instead' : 'Create new user';
  authForm.password.type = 'password';
  $('#togglePassword').textContent = '👁';
}

authForm.addEventListener('submit', async e => {
  e.preventDefault(); clearErrors(authForm);
  const payload = Object.fromEntries(new FormData(authForm));
  const button = $('#authSubmit'); button.disabled = true; button.textContent = creatingUser ? 'Creating…' : 'Logging in…';
  try {
    const res = await fetch(creatingUser ? '/api/users' : '/api/login', apiOptions({method:'POST', body:JSON.stringify(payload)}));
    const data = await res.json();
    if(!res.ok){ showErrors(authForm, data.fields); toast(data.error || 'Could not continue.'); return; }
    if (creatingUser) { toast('User created. Please log in.'); setAuthMode(false); authForm.username.value = payload.username; authForm.password.focus(); return; }
    authToken = data.token; signedInName = data.username;
    localStorage.setItem('inventoryToken', authToken); localStorage.setItem('inventoryUser', signedInName);
    toast('Logged in.'); showApp();
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled=false; button.textContent=creatingUser?'Create user':'Log in'; }
});

form.addEventListener('submit', async e => {
  e.preventDefault(); clearErrors(form);
  const payload=Object.fromEntries(new FormData(form));
  const button=$('#saveItem'); button.disabled=true; button.textContent='Saving…';
  try {
    const res=await fetch(editingId ? `/api/items/${editingId}` : '/api/items',apiOptions({method:editingId?'PUT':'POST',body:JSON.stringify(payload)}));
    const data=await res.json();
    if(res.status === 401){ showLogin(); toast('Please log in.'); return; }
    if(!res.ok){ showErrors(form, data.fields); toast(data.error || 'Could not save item.'); return; }
    dialog.close(); showNotificationResult(data.notification); toast(editingId ? 'Item updated.' : 'Item added.'); loadItems();
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled=false; button.textContent=editingId?'Save changes':'Save item'; }
});
sevaForm.addEventListener('submit', async e => {
  e.preventDefault(); clearErrors(sevaForm);
  const payload = sevaPayloadFrom(sevaForm);
  const button = $('#saveSeva'); button.disabled = true; button.textContent = 'Submitting…';
  try {
    const res = await fetch('/api/sevas', apiOptions({method:'POST', body:JSON.stringify(payload)}));
    const data = await res.json();
    if(res.status === 401){ showLogin(); toast('Please log in.'); return; }
    if(!res.ok){ showErrors(sevaForm, data.fields); toast(data.error || 'Could not save seva.'); return; }
    sevaDialog.close(); showNotificationResult(data.notification); toast('Seva saved. Inventory updated.'); loadItems();
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled = false; button.textContent = 'Submit seva'; }
});
sevaEditList.addEventListener('click', e => {
  const choice = e.target.closest('[data-seva-choice]');
  if (choice) openSelectedSeva(Number(choice.dataset.sevaChoice));
});
sevaEditForm.addEventListener('submit', async e => {
  e.preventDefault(); if (sevaPanelMode === 'none') return; clearErrors(sevaEditForm);
  const payload = sevaPayloadFrom(sevaEditForm, '[data-edit-seva-item-id]');
  const button = $('#saveSevaEdit'); button.disabled = true; button.textContent = 'Saving…';
  try {
    const isAdd = sevaPanelMode === 'add';
    const res = await fetch(isAdd ? '/api/sevas' : `/api/sevas/${editingSevaId}`, apiOptions({method:isAdd ? 'POST' : 'PUT', body:JSON.stringify(payload)}));
    const data = await res.json();
    if(res.status === 401){ showLogin(); return; }
    if(!res.ok){ showErrors(sevaEditForm, data.fields); toast(data.error || 'Could not save seva.'); return; }
    showNotificationResult(data.notification); toast(isAdd ? 'Seva added. Inventory updated.' : 'Seva updated. Inventory adjusted.');
    await loadItems(); await loadSevas(); renderSevaEditList(); openSelectedSeva(data.id || editingSevaId);
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled = false; button.textContent = 'Save seva'; }
});
$('#deleteSeva').addEventListener('click', async () => {
  if (!editingSevaId || !confirm('Delete this seva and restore its inventory quantities?')) return;
  const button = $('#deleteSeva'); button.disabled = true; button.textContent = 'Deleting…';
  try {
    const res = await fetch(`/api/sevas/${editingSevaId}`, apiOptions({method:'DELETE'}));
    const data = await res.json();
    if(res.status === 401){ showLogin(); return; }
    if(!res.ok){ toast(data.error || 'Could not delete seva.'); return; }
    editingSevaId = null; sevaPanelMode = 'none'; showNotificationResult(data.notification); toast('Seva deleted. Inventory restored.'); await loadItems(); await loadSevas(); renderSevaEditList();
    sevaEditPanel.innerHTML = '<div class="seva-empty">Select a seva to edit, or add a new seva.</div>'; $('#saveSevaEdit').disabled = true;
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled = !editingSevaId; button.textContent = 'Delete seva'; }
});
inventoryEditForm.addEventListener('submit', async e => {
  e.preventDefault(); clearErrors(inventoryEditForm);
  const updates = items.map(item => {
    const quantity = inventoryEditForm.elements[`quantity_${item.id}`];
    const reorder = inventoryEditForm.elements[`reorder_${item.id}`];
    return {...item, quantity: Number(quantity?.value ?? item.quantity), reorder_level: Number(reorder?.value ?? item.reorder_level)};
  });
  const fieldErrors = {};
  updates.forEach(item => {
    if (!Number.isInteger(item.quantity) || item.quantity < 0) fieldErrors[`quantity_${item.id}`] = 'Use 0 or more.';
    if (!Number.isInteger(item.reorder_level) || item.reorder_level < 0) fieldErrors[`reorder_${item.id}`] = 'Use 0 or more.';
  });
  if (Object.keys(fieldErrors).length) { showErrors(inventoryEditForm, fieldErrors); toast('Please correct the highlighted values.'); return; }
  const button = $('#saveInventoryEdit'); button.disabled = true; button.textContent = 'Saving…';
  try {
    for (const item of updates) {
      const original = items.find(current => current.id === item.id);
      if (original.quantity === item.quantity && original.reorder_level === item.reorder_level) continue;
      const res = await fetch(`/api/items/${item.id}`, apiOptions({method:'PUT', body:JSON.stringify(item)}));
      const data = await res.json();
      if(res.status === 401){ showLogin(); toast('Please log in.'); return; }
      if(!res.ok){ toast(data.error || `Could not update ${item.name}.`); return; }
      showNotificationResult(data.notification);
    }
    inventoryEditDialog.close(); toast('Inventory updated.'); loadItems();
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled = false; button.textContent = 'Save inventory'; }
});
body.addEventListener('click', e => {
  const edit=e.target.dataset.edit, del=e.target.dataset.delete;
  if(edit) openForm(items.find(i=>i.id===Number(edit)));
  if(del){ deletingId=Number(del); const item=items.find(i=>i.id===deletingId); $('#deleteName').textContent=item.name; deleteDialog.showModal(); }
});
$('#confirmDelete').addEventListener('click', async () => {
  const res=await fetch(`/api/items/${deletingId}`,apiOptions({method:'DELETE'}));
  if(res.ok){deleteDialog.close();toast('Item deleted.');loadItems();} else toast('Could not delete item.');
});
$('#togglePassword').onclick=()=>{ const input=authForm.password; const showing=input.type==='text'; input.type=showing?'password':'text'; $('#togglePassword').textContent=showing?'👁':'🙈'; input.focus(); };
$('#switchAuth').onclick=()=>setAuthMode(!creatingUser);
$('#logout').onclick=async()=>{ try{ await fetch('/api/logout', apiOptions({method:'POST'})); } finally { showLogin(); toast('Logged out.'); } };
$('#addItem').onclick=()=>openForm(); $('#emptyAdd').onclick=()=>openForm(); $('#editInventory').onclick=()=>openInventoryEditForm(); $('#sevaMaster').onclick=()=>openSevaEdit(); $('#addSevaInMaster').onclick=()=>openAddSevaInMaster();
$('#closeDialog').onclick=()=>dialog.close(); $('#cancelDialog').onclick=()=>dialog.close();
$('#closeSevaDialog').onclick=()=>sevaDialog.close(); $('#cancelSevaDialog').onclick=()=>sevaDialog.close();
$('#closeInventoryEditDialog').onclick=()=>inventoryEditDialog.close(); $('#cancelInventoryEditDialog').onclick=()=>inventoryEditDialog.close();
$('#closeSevaLogDialog').onclick=()=>sevaLogDialog.close(); $('#doneSevaLog').onclick=()=>sevaLogDialog.close();
$('#closeSevaEditDialog').onclick=()=>sevaEditDialog.close(); $('#cancelSevaEditDialog').onclick=()=>sevaEditDialog.close();
$('#cancelDelete').onclick=()=>deleteDialog.close();
$('#search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(loadItems,250)});
$('#statusFilter').addEventListener('change',loadItems);
if (authToken) showApp(); else showLogin();
