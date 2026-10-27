/**
 * 줌 수업 복습 — Apps Script 서버 (GitHub Pages 화면용 API)
 *
 * 하는 일: 앱에서 올린 줌 녹화를 구글 드라이브에 잠시 보관 → GitHub Actions가 가져가 정리
 *          복습 진도 저장 (휴대폰·PC 어디서 열어도 같은 진도)
 *
 * 처음 한 번: 위쪽 함수 목록에서 'setup' 선택 → 실행 → 권한 허용 → 실행 로그에 나온 '앱 비밀번호' 메모
 * 입트영 Apps Script와는 완전히 별개인 새 프로젝트에 넣습니다.
 * 그다음: 배포 → 새 배포 → 유형 '웹 앱' / 실행 사용자: 나 / 액세스 권한: 모든 사용자 → 배포
 *         → 나온 웹 앱 URL(…/exec)을 GitHub 저장소 config.js에 붙여 넣기
 *
 * 스크립트 속성(프로젝트 설정 → 스크립트 속성)
 *   ZOOM_KEY      앱 비밀번호 (setup이 자동으로 만들고 실행 로그에 보여 줌). GitHub 저장소 Secrets에도 같은 값
 *   GITHUB_TOKEN  (선택) 넣으면 업로드 즉시 정리 시작 (없으면 15분 안에 자동 시작)
 *   GITHUB_REPO   (선택) GITHUB_TOKEN과 함께: 아이디/저장소 (예: honggildong/zoom-review)
 */

const PROPS = PropertiesService.getScriptProperties();
const ROOT_NAME = '줌 수업 복습';
const API_FNS = ['zoom_check', 'zoom_uploadPart', 'zoom_finishUpload', 'zoom_jobs', 'zoom_pendingJobs', 'zoom_getPart', 'zoom_setJob', 'zoom_deleteJob', 'zoom_getProgress', 'zoom_saveProgress'];

/* ---------- API ---------- */
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
function doPost(e) {
  let b = {};
  try { b = JSON.parse(e.postData.contents || '{}'); } catch (x) { return json_({ err: 'bad_request' }); }
  if (API_FNS.indexOf(b.fn) < 0) return json_({ err: 'unknown_fn' });
  const args = b.args || [];
  if (String(args[0] || '') !== String(PROPS.getProperty('ZOOM_KEY') || '#')) return json_({ err: 'wrong_key' });
  try { return json_({ res: this[b.fn].apply(null, args.slice(1)) }); }
  catch (x) { return json_({ err: String(x && x.message || x) }); }
}
function doGet() { return json_({ ok: true, app: '줌 수업 복습 API' }); }

/* ---------- 최초 설정 ---------- */
function setup() {
  let root;
  const rid = PROPS.getProperty('ROOT_ID');
  if (rid) { try { root = DriveApp.getFolderById(rid); } catch (e) { root = null; } }
  if (!root) { const it = DriveApp.getFoldersByName(ROOT_NAME); root = it.hasNext() ? it.next() : DriveApp.createFolder(ROOT_NAME); }
  const up = sub_(root, 'uploads');
  PROPS.setProperties({ ROOT_ID: root.getId(), UP_ID: up.getId() });
  if (!PROPS.getProperty('ZOOM_KEY')) PROPS.setProperty('ZOOM_KEY', 'zoom-' + Math.floor(Math.random() * 900000 + 100000));
  Logger.log('설정 완료. 폴더: %s', root.getUrl());
  Logger.log('앱 비밀번호(ZOOM_KEY): %s  ← 앱 첫 화면과 GitHub Secrets에 같은 값을 넣으세요', PROPS.getProperty('ZOOM_KEY'));
}

/* ---------- 화면이 부르는 함수 ---------- */
function zoom_check() { return true; }

/* 녹화 파일을 4MB씩 나눠 받음 (Apps Script 한 번에 받을 수 있는 크기 제한 때문) */
function zoom_uploadPart(id, i, b64) {
  const f = sub_(folder_('UP_ID'), safe_(id));
  const name = 'part-' + ('000' + i).slice(-4) + '.txt';
  const it = f.getFilesByName(name); while (it.hasNext()) it.next().setTrashed(true);
  f.createFile(name, b64, 'text/plain');
  return i;
}
function zoom_finishUpload(id, date, name, parts, size) {
  const lock = LockService.getScriptLock(); lock.waitLock(20000);
  try {
    const J = readJobs_();
    J[id] = { id: id, date: date, name: name, parts: parts, size: size, status: 'waiting', created: new Date().toISOString() };
    writeJobs_(J);
  } finally { lock.releaseLock(); }
  kick_();
  return true;
}
/* 앱 화면: 최근 작업 상태 (대기·정리 중·완료·오류) */
function zoom_jobs() {
  const J = readJobs_();
  return Object.keys(J).map(k => J[k]).sort((a, b) => b.created.localeCompare(a.created)).slice(0, 10);
}
function zoom_deleteJob(id) {
  const J = readJobs_(); delete J[id]; writeJobs_(J); clearParts_(id); return true;
}

/* GitHub Actions가 부르는 함수 */
function zoom_pendingJobs() {
  const J = readJobs_(), now = Date.now();
  return Object.keys(J).map(k => J[k]).filter(j =>
    j.status === 'waiting' || (j.status === 'processing' && now - new Date(j.started || j.created).getTime() > 2 * 3600 * 1000)
  ).sort((a, b) => a.created.localeCompare(b.created));
}
function zoom_getPart(id, i) {
  const f = sub_(folder_('UP_ID'), safe_(id));
  const it = f.getFilesByName('part-' + ('000' + i).slice(-4) + '.txt');
  if (!it.hasNext()) throw new Error('part_missing ' + i);
  return it.next().getBlob().getDataAsString();
}
function zoom_setJob(id, patch) {
  const lock = LockService.getScriptLock(); lock.waitLock(20000);
  try {
    const J = readJobs_(); if (!J[id]) return false;
    Object.keys(patch || {}).forEach(k => J[id][k] = patch[k]);
    if (patch.status === 'processing') J[id].started = new Date().toISOString();
    if (patch.status === 'done') J[id].finished = new Date().toISOString();
    writeJobs_(J);
  } finally { lock.releaseLock(); }
  if (patch && patch.status === 'done') clearParts_(id);
  return true;
}

/* 진도: { '2026-10-23:Lt1': 'ok', ... } 하나의 파일로 저장 */
function zoom_getProgress() { return readJson_(folder_('ROOT_ID'), 'progress.json') || {}; }
function zoom_saveProgress(p) {
  const lock = LockService.getScriptLock(); lock.waitLock(20000);
  try {
    const cur = readJson_(folder_('ROOT_ID'), 'progress.json') || {};
    Object.keys(p || {}).forEach(k => { if (p[k] === null) delete cur[k]; else cur[k] = p[k]; });
    writeJson_(folder_('ROOT_ID'), 'progress.json', cur);
    return true;
  } finally { lock.releaseLock(); }
}

/* ---------- 내부 도우미 ---------- */
function kick_() {
  const tok = PROPS.getProperty('GITHUB_TOKEN'), repo = PROPS.getProperty('GITHUB_REPO');
  if (!tok || !repo) return;
  try {
    UrlFetchApp.fetch('https://api.github.com/repos/' + repo + '/dispatches', {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      headers: { Authorization: 'Bearer ' + tok, Accept: 'application/vnd.github+json' },
      payload: JSON.stringify({ event_type: 'zoom' })
    });
  } catch (e) {}
}
function readJobs_() { return readJson_(folder_('ROOT_ID'), 'jobs.json') || {}; }
function writeJobs_(J) { writeJson_(folder_('ROOT_ID'), 'jobs.json', J); }
function clearParts_(id) { const it = folder_('UP_ID').getFoldersByName(safe_(id)); while (it.hasNext()) it.next().setTrashed(true); }
function folder_(k) { const id = PROPS.getProperty(k); if (!id) throw new Error('setup_needed'); return DriveApp.getFolderById(id); }
function sub_(parent, name) { const it = parent.getFoldersByName(name); return it.hasNext() ? it.next() : parent.createFolder(name); }
function safe_(k) { return String(k || 'x').replace(/[^A-Za-z0-9_\-]/g, '_').slice(0, 80); }
function readJson_(folder, name) { const it = folder.getFilesByName(name); if (!it.hasNext()) return null; try { return JSON.parse(it.next().getBlob().getDataAsString('UTF-8')); } catch (e) { return null; } }
function writeJson_(folder, name, obj) {
  const s = JSON.stringify(obj), it = folder.getFilesByName(name);
  if (it.hasNext()) it.next().setContent(s); else folder.createFile(name, s, 'application/json');
}
