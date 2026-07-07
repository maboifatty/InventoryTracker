const $ = (s) => document.querySelector(s);
const body = $('#itemsBody'), empty = $('#emptyState'), form = $('#itemForm');
const dialog = $('#itemDialog'), deleteDialog = $('#deleteDialog'), authForm = $('#authForm');
let items = [], editingId = null, deletingId = null, timer, creatingUser = false;
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
  $('#resultCount').textContent = `${items.length} product${items.length === 1 ? '' : 's'}`;
  body.innerHTML = items.map(item => { const status=stockStatus(item); return `<tr>
    <td><div class="product"><strong>${esc(item.name)}</strong><small>${esc(item.sku)}</small></div></td>
    <td><span class="qty">${number.format(item.quantity)}</span></td>
    <td><span class="badge ${status[1]}">${status[0]}</span></td>
    <td class="row-actions"><button class="action-btn" data-edit="${item.id}" title="Edit item">Edit</button><button class="action-btn" data-delete="${item.id}" title="Delete item">Delete</button></td></tr>`; }).join('');
  empty.hidden = items.length !== 0; body.closest('table').hidden = items.length === 0;
}
function openForm(item=null) {
  editingId = item?.id || null; form.reset(); clearErrors(form);
  $('#dialogTitle').textContent = item ? 'Edit inventory item' : 'Add inventory item';
  $('#saveItem').textContent = item ? 'Save changes' : 'Save item';
  if (item) Object.keys(item).forEach(k => { if (form.elements[k]) form.elements[k].value = item[k]; });
  else { form.quantity.value=0; form.reorder_level.value=0; }
  dialog.showModal(); setTimeout(() => form.name.focus(), 30);
}
function clearErrors(scope=document) { scope.querySelectorAll('.error').forEach(e=>e.textContent=''); scope.querySelectorAll('.invalid').forEach(e=>e.classList.remove('invalid')); }
function showErrors(scope, fields={}) { clearErrors(scope); Object.entries(fields || {}).forEach(([name,msg])=>{ const input=scope.elements[name]; if(input){input.classList.add('invalid');input.closest('label').querySelector('.error').textContent=msg;}}); }
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
    dialog.close(); toast(editingId ? 'Item updated.' : 'Item added.'); loadItems();
  } catch { toast('Could not connect to the inventory service.'); }
  finally { button.disabled=false; button.textContent=editingId?'Save changes':'Save item'; }
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
$('#addItem').onclick=()=>openForm(); $('#emptyAdd').onclick=()=>openForm();
$('#closeDialog').onclick=()=>dialog.close(); $('#cancelDialog').onclick=()=>dialog.close();
$('#cancelDelete').onclick=()=>deleteDialog.close();
$('#search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(loadItems,250)});
$('#statusFilter').addEventListener('change',loadItems);
if (authToken) showApp(); else showLogin();
