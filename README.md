# 줌 수업 복습

매일 필리핀 강사와 하는 줌 영어 수업 녹화를 올리면, 강사 목소리로 듣기·받아쓰기·말하기·표현 카드를 날짜별로 만들어 주는 복습 앱입니다.

- **화면**: GitHub Pages → `https://junkim10293847.github.io/zoom-review/`
- **녹화 보관·진도 저장**: 줌 복습 전용 Apps Script (관리자 구글 드라이브 `줌 수업 복습` 폴더)
- **자동 정리**: GitHub Actions가 15분마다 새 녹화를 확인 → 강사/학습자 목소리 분리 → 영어 받아쓰기 → Gemini로 강사 말 번역·끊어 읽기, 내 말 추출·오류 교정, 표현 정리

## 처음 설정 (한 번만)

입트영 앱과 완전히 별개인 **새 Apps Script 프로젝트**를 씁니다. 입트영 코드는 건드리지 않습니다.

1. [script.google.com](https://script.google.com) → **새 프로젝트** → 이름 `줌 수업 복습` → `apps-script/Code.gs` 내용을 통째로 붙여 넣고 저장합니다.
2. 함수 목록에서 `setup` 선택 → 실행 → 권한 허용. 실행 로그에 나온 **앱 비밀번호(ZOOM_KEY)** 를 메모합니다.
3. **배포 → 새 배포 → 유형: 웹 앱** (실행: 나 / 액세스: 모든 사용자) → 나온 웹 앱 URL(…/exec)을 `config.js`에 붙여 넣습니다.
4. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에 두 개를 넣습니다.
   - `ZOOM_KEY`: 2번의 앱 비밀번호
   - `GEMINI_API_KEY`: Google AI Studio 무료 키
5. **Settings → Pages → Branch: main, / (root) → Save**

## 매일 할 일

수업이 끝나면 앱에서 **+ 수업 올리기** → 줌 녹화(mp4 또는 m4a) 선택 → 올리기. 20~30분 뒤 목록에 그 날짜로 복습 카드가 생깁니다.
바로 시작하고 싶으면 **Actions → 줌 수업 자동 정리 → Run workflow** 를 누르세요.

## 참고

- 수업 녹화의 음성(32kbps)과 정리된 문장은 저장소(공개)에 올라갑니다. 원본 녹화는 정리가 끝나면 드라이브에서 지워집니다.
- 저장소에 60일 동안 변화가 없으면 GitHub가 15분 자동 확인을 멈춥니다. 그때는 Actions 탭에서 다시 켜면 됩니다.
- 시험 실행: `python scripts/process.py --file 녹화.mp4 --date 2026-10-27` (음성 도구가 `.sherpa` 폴더에 있어야 함)
