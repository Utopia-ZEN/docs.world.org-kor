# docs.world.org Korean Mirror (자동 번역)

`https://docs.world.org/` 문서를 매일 1회 자동 수집하여, **코드 블록/식별자/명령어를 보존**하면서 한국어로 번역한 정적 사이트를 생성합니다.

## 핵심 동작

- 사이트맵(`sitemap.xml`) 기반 URL 수집
- 문서 본문 텍스트만 번역 (`main/article/theme-doc-markdown` 우선)
- `code`, `pre`, `kbd`, `samp`, `script`, `style` 태그 번역 제외
- `header/nav/footer/aside` 등 UI 컨테이너는 기본 제외
- 번역 캐시(문장+모델 해시 키)로 중복 비용 절감
- 내부 링크를 로컬 경로로 보정하여 미러 사이트에서 자연스럽게 이동
- `output/` 정적 HTML + `summary.json` 생성

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export OPENAI_API_KEY="<YOUR_KEY>"
python scripts/translate_site.py \
  --base-url https://docs.world.org \
  --sitemap-url https://docs.world.org/sitemap.xml \
  --max-urls 500
```

주요 옵션:

- `--openai-model` (기본: `gpt-4.1-mini`)
- `--openai-base-url` (기본: `https://api.openai.com/v1`)
- `--openai-max-retries` (기본: `4`)
- `--request-timeout` (기본: `30`)

출력:

- `output/index.html`
- `output/**/index.html`
- `output/summary.json`
- `.translation-cache.json`

## 자동 실행 (GitHub Actions)

1. Repository Secrets에 `OPENAI_API_KEY` 추가
2. Actions 활성화
3. Pages 소스를 `GitHub Actions`로 설정

워크플로우:

- `.github/workflows/daily-translate.yml`
- 스케줄: `0 2 * * *` (UTC) = 매일 11:00 KST

## 테스트

```bash
python -m py_compile scripts/translate_site.py
python -m unittest discover -s tests -p 'test_*.py'
```
