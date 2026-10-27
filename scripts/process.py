"""줌 수업 녹화 자동 정리 (GitHub Actions에서 실행)

1) Apps Script에 올라온 새 녹화가 있는지 확인하고
2) 녹화를 내려받아 강사/학습자 목소리를 나누고 (sherpa-onnx 화자 분리)
3) 구간별로 영어 받아쓰기를 하고 (Whisper small.en)
4) Gemini로 강사 문장 정리·번역·끊어 읽기·표현·말하기 문장을 만든 뒤
5) data/lessons/<날짜>.json, audio/<날짜>.mp3, data/index.json을 저장합니다.

  python scripts/process.py --plan            # 처리할 녹화가 있는지만 확인
  python scripts/process.py                   # 가장 오래된 녹화 1개 처리
  python scripts/process.py --file 녹화.mp4 --date 2026-10-23   # 내 컴퓨터 파일로 직접 처리(시험용)

필요한 환경변수: ZOOM_KEY(앱 비밀번호), GEMINI_API_KEY
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.request, wave

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TOOLS = os.environ.get('SHERPA_DIR', os.path.join(ROOT, '.sherpa'))
WORK = os.path.join(ROOT, '.work')
GEMINI_CANDIDATES = ['gemini-2.5-flash', 'gemini-flash-latest', 'gemini-3.6-flash', 'gemini-3.8-flash',
                     'gemini-2.5-flash-lite', 'gemini-flash-lite-latest', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']


def out(k, v):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write(f'{k}={v}\n')
    print(f'{k}={v}')


# ---------- Apps Script ----------
def api_url():
    src = open(os.path.join(ROOT, 'config.js'), encoding='utf-8').read()
    m = re.search(r'api\s*:\s*["\']([^"\']+)["\']', src)
    url = m and m.group(1)
    return url if url and (url.startswith('https://') or url.startswith('http://127.0.0.1')) else None


def api(fn, *args):
    url = api_url()
    body = json.dumps({'fn': fn, 'args': [os.environ.get('ZOOM_KEY', '')] + list(args)}).encode('utf-8')
    last = None
    for t in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={'Content-Type': 'text/plain;charset=utf-8', 'User-Agent': 'zoom-review'})
            with urllib.request.urlopen(req, timeout=180) as r:
                j = json.loads(r.read().decode('utf-8'))
            if j.get('err'):
                raise RuntimeError('Apps Script 오류: ' + j['err'])
            return j.get('res')
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            print(f'서버 응답 없음({t + 1}/4): {e}')
            time.sleep(10 * (t + 1))
    raise RuntimeError(f'서버 연결 실패: {last}')


# ---------- 음성 처리 ----------
def run(cmd, **kw):
    env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(TOOLS, 'lib'))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env, **kw)


def to_wav(src, dst):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-ac', '1', '-ar', '16000', dst], check=True)


def diarize(wav):
    r = run([os.path.join(TOOLS, 'bin', 'sherpa-onnx-offline-speaker-diarization'), '--clustering.num-clusters=2',
             '--segmentation.pyannote-model=' + os.path.join(TOOLS, 'models', 'segmentation.onnx'),
             '--embedding.model=' + os.path.join(TOOLS, 'models', 'embedding.onnx'), wav])
    segs = []
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 4 and p[1] == '--':
            s, e, spk = float(p[0]), float(p[2]), p[3]
            if segs and segs[-1][2] == spk and s - segs[-1][1] < 0.8 and e - segs[-1][0] < 25:
                segs[-1][1] = e
            else:
                segs.append([s, e, spk])
    return segs


def transcribe(wav, segs):
    os.makedirs(os.path.join(WORK, 'segs'), exist_ok=True)
    w = wave.open(wav); sr = w.getframerate(); sw = w.getsampwidth(); data = w.readframes(w.getnframes())
    files = []
    for i, (s, e, _) in enumerate(segs):
        fn = os.path.join(WORK, 'segs', f'{i:04d}.wav')
        o = wave.open(fn, 'wb'); o.setnchannels(1); o.setsampwidth(sw); o.setframerate(sr)
        o.writeframes(data[int(max(0, s - 0.15) * sr) * sw:int((e + 0.15) * sr) * sw]); o.close()
        files.append(fn)
    M = os.path.join(TOOLS, 'models')
    texts = []
    for k in range(0, len(files), 60):                      # 명령줄 길이 제한을 피해 60개씩
        r = run([os.path.join(TOOLS, 'bin', 'sherpa-onnx-offline'),
                 '--whisper-encoder=' + os.path.join(M, 'whisper-encoder.onnx'),
                 '--whisper-decoder=' + os.path.join(M, 'whisper-decoder.onnx'),
                 '--tokens=' + os.path.join(M, 'whisper-tokens.txt'), '--whisper-language=en',
                 '--num-threads=' + str(os.cpu_count() or 2)] + files[k:k + 60])
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith('{'):
                texts.append(json.loads(line).get('text', '').strip())
    if len(texts) != len(segs):
        raise RuntimeError(f'받아쓰기 개수 불일치 {len(texts)} / {len(segs)}')
    noise = re.compile(r'^[\[\(\*].*[\]\)\*]?$')
    rows = []
    for (s, e, spk), t in zip(segs, texts):
        if t and not noise.match(t):
            rows.append({'s': round(s, 2), 'e': round(e, 2), 'spk': spk, 't': t})
    # 강사 = 영어를 더 많이 말한 사람
    cnt = {}
    for r_ in rows:
        cnt[r_['spk']] = cnt.get(r_['spk'], 0) + len(r_['t'].split())
    teacher = max(cnt, key=cnt.get) if cnt else None
    for i, r_ in enumerate(rows):
        r_['id'] = i
        r_['who'] = 'T' if r_['spk'] == teacher else 'S'
    return rows


# ---------- Gemini ----------
def gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY가 없습니다 (저장소 Settings → Secrets에 넣어 주세요)')
    fixed = [x.strip() for x in os.environ.get('GEMINI_MODEL', '').split(',') if x.strip()]
    last = 'empty'
    for model in fixed + [m for m in GEMINI_CANDIDATES if m not in fixed]:
        gc = {'responseMimeType': 'application/json', 'temperature': 0.3}
        if model.startswith('gemini-2.5'):
            gc['thinkingConfig'] = {'thinkingBudget': 0}
        body = json.dumps({'contents': [{'role': 'user', 'parts': [{'text': prompt}]}], 'generationConfig': gc}).encode()
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
                with urllib.request.urlopen(req, timeout=300) as r:
                    j = json.loads(r.read().decode('utf-8'))
                text = ''.join(p.get('text', '') for p in j['candidates'][0]['content']['parts'])
                return json.loads(re.sub(r'^```(json)?|```$', '', text.strip()))
            except urllib.error.HTTPError as e:
                last = f'{model} {e.code}'
                if e.code in (404, 400, 429) or e.code >= 500:
                    break
            except Exception as e:
                last = f'{model} {e}'
                time.sleep(5)
        print('Gemini 다음 모델로:', last)
    raise RuntimeError('Gemini 실패: ' + last)


def mmss(x):
    return f'{int(x // 60):02d}:{int(x % 60):02d}'


PROMPT_LINES = """You are helping a Korean adult learner review a 1:1 English conversation class on Zoom with a Filipino teacher.
Below is a machine transcript. Each row is [id] speaker time: text. T = teacher, S = student (the learner).
The transcript has recognition errors, especially for S. Use context to repair obvious mistakes.

Task: build listening-review cards from the TEACHER's speech.
- Group teacher rows into natural sentences worth reviewing (skip fillers like "Mm", "Yeah", "Okay" alone). Max 40 lines, in time order.
- For each line give "segs": the list of teacher row ids it comes from (consecutive, total under 25 seconds).
- "en": the cleaned English as the teacher meant it (fix recognition errors, keep her wording and level). If a word is truly uncertain, append " [?]".
- "ko": natural Korean translation (존댓말).
- "chunks": split "en" into 2-5 meaning chunks in English order, each [english chunk, Korean meaning of that chunk] (직독직해).
- "words": 0-3 harder words or patterns in the line, each [word or pattern, short Korean meaning].
- "core": true for the 12 most useful lines for a learner (clear, reusable sentences). Exactly 12 if there are 12+ lines.
- "dict": true for lines short and clear enough for dictation (about 5-15 words).

Also return:
- "topic": short English title of the class topic. "topicKo": Korean title.
- "teacher": the teacher's name if it appears (e.g. "Teacher Mina"), otherwise "Teacher".
- "summary": 1-2 Korean sentences summarising what was talked about.

Return JSON only: {"topic":"","topicKo":"","teacher":"","summary":"","lines":[{"segs":[0],"en":"","ko":"","chunks":[["",""]],"words":[["",""]],"core":true,"dict":true}]}

TRANSCRIPT:
"""

PROMPT_SPEAK = """You are an English coach for a Korean adult learner (a company executive) who takes daily 20-minute Zoom conversation classes with a Filipino teacher.
Below is the class transcript (T = teacher, S = student; machine transcript, S has many recognition errors — infer what S tried to say from context).

Return JSON only:
{"fixes":[{"mine":"what S actually said (short, as in transcript)","better":"natural English for what S meant","ko":"Korean meaning","tip":"one short Korean tip on the point"}],
 "phrases":[{"en":"useful expression from the class","ko":"Korean meaning","ex":"example sentence from the class"}],
 "next":{"q":"one follow-up discussion question in English the teacher could ask next time on this topic","ko":"Korean translation","starters":[{"en":"","ko":""}]}}
Rules: 6-10 fixes (most useful first), 8-10 phrases, 5 starters that the learner could say to answer "q" using his own stories from this class. Korean in 존댓말-free plain style is fine for tips.

TRANSCRIPT:
"""

PROMPT_MINE = """You are an English coach for a Korean adult learner who takes daily 1:1 Zoom conversation classes with a Filipino teacher.
Below is the machine transcript. Each row is [id] speaker time: text. T = teacher, S = the learner.
Speech recognition of S is poor: use the teacher's replies and context to work out what S actually said and meant.

Task: list everything meaningful the LEARNER (S) said, in time order, and correct his English.
- Group consecutive S rows that form one answer into one item (under 40 seconds). Skip pure fillers ("Yes", "Mm", "Thank you", "Oh").
- "segs": the S row ids used.
- "said": what S actually said, cleaned only of recognition noise — KEEP his grammar and word-choice mistakes exactly (this is what he will compare against).
- "better": natural, simple English for what he meant (a level he can say himself, not fancy).
- "ko": Korean meaning of "better".
- "errors": each mistake as {"wrong": the part he said, "right": the fix, "why": one short Korean explanation (grammar point, word choice, tense, article, preposition, pronunciation confusion)}. Empty list if the sentence was fine.
- Up to 25 items.

Return JSON only: {"mine":[{"segs":[0],"said":"","better":"","ko":"","errors":[{"wrong":"","right":"","why":""}]}]}

TRANSCRIPT:
"""


def organize(rows):
    tx = '\n'.join(f"[{r['id']}] {r['who']} {mmss(r['s'])}: {r['t']}" for r in rows)
    A = gemini(PROMPT_LINES + tx)
    B = gemini(PROMPT_SPEAK + tx)
    try:
        C = gemini(PROMPT_MINE + tx)
    except Exception as e:                       # 내 말 교정이 실패해도 나머지는 저장
        print('내 말 교정 실패:', e); C = {}
    by = {r['id']: r for r in rows}
    mine = []
    for i, x in enumerate(C.get('mine', [])):
        ids = [k for k in (x.get('segs') or []) if k in by]
        if not ids or not x.get('said'):
            continue
        s = min(by[k]['s'] for k in ids) - 0.25
        e = min(max(by[k]['e'] for k in ids), s + 40) + 0.3
        mine.append({'id': f'm{i + 1}', 'start': round(max(0, s), 2), 'end': round(e, 2), 'said': x['said'],
                     'better': x.get('better', ''), 'ko': x.get('ko', ''),
                     'errors': [er for er in (x.get('errors') or []) if isinstance(er, dict) and er.get('wrong')]})
    lines = []
    for i, x in enumerate(A.get('lines', [])):
        ids = [k for k in (x.get('segs') or []) if k in by]
        if not ids or not x.get('en'):
            continue
        s = min(by[k]['s'] for k in ids) - 0.25
        e = min(max(by[k]['e'] for k in ids), s + 25) + 0.3
        lines.append({'id': f't{i + 1}', 'start': round(max(0, s), 2), 'end': round(e, 2), 'en': x['en'], 'ko': x.get('ko', ''),
                      'chunks': x.get('chunks') or [], 'words': x.get('words') or [],
                      'core': bool(x.get('core')), 'dict': bool(x.get('dict'))})
    if lines and not any(l['core'] for l in lines):
        for l in lines[:12]:
            l['core'] = True
    nx = B.get('next') or {}
    return {'topic': A.get('topic', 'English class'), 'topicKo': A.get('topicKo', ''), 'teacher': A.get('teacher', 'Teacher'),
            'summary': A.get('summary', ''), 'lines': lines, 'mine': mine,
            'fixes': B.get('fixes') or [], 'phrases': B.get('phrases') or [],
            'next': {'q': nx.get('q', ''), 'ko': nx.get('ko', ''), 'starters': nx.get('starters') or []}}


# ---------- 저장 ----------
def save(date, lesson, src, rows):
    os.makedirs(os.path.join(ROOT, 'audio'), exist_ok=True)
    os.makedirs(os.path.join(ROOT, 'data', 'lessons'), exist_ok=True)
    mp3 = f'audio/{date}.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vn', '-ac', '1', '-ar', '22050', '-b:a', '32k', os.path.join(ROOT, mp3)], check=True)
    dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', os.path.join(ROOT, mp3)],
                               capture_output=True, text=True).stdout.strip() or 0)
    lesson = dict({'date': date, 'audio': mp3, 'minutes': round(dur / 60)}, **lesson)
    json.dump(lesson, open(os.path.join(ROOT, 'data', 'lessons', f'{date}.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(rows, open(os.path.join(ROOT, 'data', 'lessons', f'{date}.transcript.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    rebuild_index()


def rebuild_index():
    d = os.path.join(ROOT, 'data', 'lessons')
    idx = []
    for fn in sorted(os.listdir(d), reverse=True):
        if re.match(r'^\d{4}-\d{2}-\d{2}\.json$', fn):
            L = json.load(open(os.path.join(d, fn), encoding='utf-8'))
            idx.append({'date': L['date'], 'topic': L.get('topic', ''), 'topicKo': L.get('topicKo', ''),
                        'teacher': L.get('teacher', ''), 'minutes': L.get('minutes'), 'n': len(L.get('lines', []))})
    json.dump({'lessons': idx}, open(os.path.join(ROOT, 'data', 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def process_file(src, date):
    os.makedirs(WORK, exist_ok=True)
    wav = os.path.join(WORK, 'audio.wav')
    to_wav(src, wav)
    print('화자 분리 중...'); segs = diarize(wav); print(len(segs), '구간')
    print('받아쓰기 중...'); rows = transcribe(wav, segs); print(len(rows), '문장')
    if os.environ.get('DUMP_ONLY'):
        json.dump(rows, open(os.path.join(WORK, 'rows.json'), 'w'), ensure_ascii=False, indent=1); return
    print('Gemini 정리 중...'); lesson = organize(rows)
    save(date, lesson, src, rows)
    print('완료:', date, len(lesson['lines']), '문장')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', action='store_true'); ap.add_argument('--file'); ap.add_argument('--date')
    a = ap.parse_args()
    if a.file:
        return process_file(a.file, a.date or time.strftime('%Y-%m-%d'))
    if not api_url():
        print('config.js에 Apps Script 주소가 없습니다.'); out('todo', 0); return
    try:
        jobs = api('zoom_pendingJobs') or []
    except Exception as e:
        print('작업 목록 확인 실패:', e); out('todo', 0); return
    out('todo', len(jobs))
    if a.plan or not jobs:
        return
    job = jobs[0]
    print('처리 시작:', job)
    api('zoom_setJob', job['id'], {'status': 'processing'})
    try:
        os.makedirs(WORK, exist_ok=True)
        ext = os.path.splitext(job.get('name') or 'x.mp4')[1] or '.mp4'
        src = os.path.join(WORK, 'input' + ext)
        with open(src, 'wb') as f:
            for i in range(int(job['parts'])):
                f.write(base64.b64decode(api('zoom_getPart', job['id'], i)))
        process_file(src, job['date'])
        api('zoom_setJob', job['id'], {'status': 'done'})
    except Exception as e:
        print('실패:', e)
        api('zoom_setJob', job['id'], {'status': 'error', 'msg': str(e)[:300]})
        raise


if __name__ == '__main__':
    main()
