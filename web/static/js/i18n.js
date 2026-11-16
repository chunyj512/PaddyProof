/* Tiny i18n: dictionaries live in js/lang/*.js and register on window.PP_LANG.
   Missing keys fall back to English, then to the key itself. */
(() => {
const LANGS = [
  {code: 'en', name: 'English', locale: 'en-GB'},
  {code: 'ko', name: '한국어', locale: 'ko-KR'},
  {code: 'vi', name: 'Tiếng Việt', locale: 'vi-VN'},
  {code: 'id', name: 'Bahasa Indonesia', locale: 'id-ID'},
  {code: 'th', name: 'ไทย', locale: 'th-TH-u-nu-latn-ca-gregory'},
];
const dict = window.PP_LANG || {};
let cur = 'en';
try { const s = localStorage.getItem('pp.lang'); if (s && dict[s]) cur = s; } catch {}
const listeners = [];

function t(key, vars) {
  let s = (dict[cur] && dict[cur][key]) ?? dict.en[key] ?? key;
  if (vars) for (const k in vars) s = s.replaceAll('{' + k + '}', vars[k]);
  return s;
}
function apply(root = document) {
  document.documentElement.lang = cur;
  root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
  root.querySelectorAll('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  const cl = document.querySelector('#langCur');
  if (cl) cl.textContent = LANGS.find(l => l.code === cur).name;
  document.querySelectorAll('#langMenu button').forEach(b => b.classList.toggle('on', b.dataset.lang === cur));
}
function set(code) {
  if (!dict[code] || code === cur) return;
  cur = code;
  try { localStorage.setItem('pp.lang', code); } catch {}
  apply();
  listeners.forEach(f => f(code));
}
function mountMenu() {
  const menu = document.querySelector('#langMenu'), btn = document.querySelector('#langBtn');
  if (!menu || !btn) return;
  menu.innerHTML = LANGS.map(l => `<button data-lang="${l.code}"><span>${l.name}</span><small>${l.code.toUpperCase()}</small></button>`).join('');
  menu.querySelectorAll('button').forEach(b => b.onclick = () => { set(b.dataset.lang); menu.classList.add('hidden'); });
  btn.onclick = e => { e.stopPropagation(); menu.classList.toggle('hidden'); };
  document.addEventListener('click', () => menu.classList.add('hidden'));
  apply();
}
window.I18N = {t, apply, set, mountMenu, lang: () => cur,
  locale: () => LANGS.find(l => l.code === cur).locale,
  onChange: f => listeners.push(f)};
})();
